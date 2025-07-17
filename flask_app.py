from flask import Flask, Response, request, stream_with_context
import subprocess
import os
import logging
import threading
import zmq
import time

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configurable constants
CDVR_HOST = os.getenv("CDVR_HOST", "192.168.86.64")
CDVR_PORT = int(os.getenv("CDVR_PORT", "8089"))
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 29.97
CODEC = os.getenv("CODEC", "libx264")
BW = '5120k'

# ZeroMQ configuration for FFmpeg control
ZMQ_FFMPEG_PORT = 5555 # Port FFmpeg will listen on for commands
# Removed global zmq_context and zmq_socket

# Global to store the active video track for highlighting (defaults to 0)
current_active_video_track = 0

def build_input_urls(channels):
    """Constructs input URLs for FFmpeg based on provided channel numbers."""
    return [f"http://{CDVR_HOST}:{CDVR_PORT}/devices/ANY/channels/{ch}/stream.mpg" for ch in channels]

@app.route('/combine')
def combine_streams():
    """
    Combines multiple Channels DVR streams into a single multi-view stream.
    Channels are specified via 'ch' query parameters (e.g., /combine?ch=1&ch=2).
    Supports up to 4 channels.
    """
    global current_active_video_track # current_active_video_track can remain global if it's a shared preference

    channels_raw = request.args.getlist('ch')
    if not channels_raw:
        logger.warning("No channels provided in the request.")
        return "No channels provided. Please use /combine?ch=1&ch=2...", 400

    channels = []
    for ch_str in channels_raw[:4]: # Limit to first 4 channels
        try:
            ch_num = int(ch_str)
            if ch_num <= 0:
                logger.warning(f"Invalid channel number provided: {ch_str}. Must be a positive integer.")
                return f"Invalid channel number: {ch_str}. Channel numbers must be positive integers.", 400
            channels.append(str(ch_num))
        except ValueError:
            logger.warning(f"Invalid channel format provided: {ch_str}. Must be an integer.")
            return f"Invalid channel format: {ch_str}. Channel numbers must be integers.", 400

    if not channels:
        logger.warning("No valid channels found after parsing input.")
        return "No valid channels found after parsing input.", 400

    urls = build_input_urls(channels)
    num_inputs = len(urls)

    ffmpeg_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info']

    for url in urls:
        ffmpeg_cmd += ['-i', url]

    # --- Video Filtergraph ---
    video_filter_parts = []
    video_input_labels = []

    sub_width = TARGET_WIDTH
    sub_height = TARGET_HEIGHT

    for i in range(num_inputs):
        video_filter_parts.append(
            f'[{i}:v]fps={TARGET_FPS},scale={TARGET_WIDTH}:{TARGET_HEIGHT},'
            f'drawbox=x=0:y=0:w=iw:h=ih:color=red@0.8:thickness=5:enable=\'eq(1,{1 if i == current_active_video_track else 0})\'[v{i}]'
        )
        video_input_labels.append(f'[v{i}]')

    layout_map = {
        1: "[v0]xstack=inputs=1:layout=0_0[final_video]",
        2: "[v0][v1]xstack=inputs=2:layout=0_0|w0_0[final_video]",
        3: "[v0][v1][v2]xstack=inputs=3:layout=0_0|w0_0|0_h0[final_video]",
        4: "[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[final_video]"
    }
    try:
        video_filter_parts.append(layout_map[num_inputs])
    except KeyError:
        logger.error(f"Unsupported number of inputs for xstack layout: {num_inputs}")
        return f"Unsupported number of channels: {num_inputs}. Max 4 channels allowed.", 500

    # --- Audio Filtergraph (for dynamic switching) ---
    audio_filter_parts = []
    audio_input_labels = []
    initial_active_audio_track = current_active_video_track

    for i in range(num_inputs):
        audio_filter_parts.append(
            f'[{i}:a]aformat=channel_layouts=stereo,volume=enable=\'eq(1,{1 if i == initial_active_audio_track else 0})\':eval=frame[a{i}]'
        )
        audio_input_labels.append(f'[a{i}]')

    audio_filter_parts.append(
        f"{''.join(audio_input_labels)}amix=inputs={num_inputs}:dropout_transition=0:duration=first[mixed_audio]"
    )

    # Corrected azmq filter syntax: passing 'control=1' as a URL query parameter
    audio_filter_parts.append(
        f"[mixed_audio]azmq=bind_address=tcp://127.0.0.1:{ZMQ_FFMPEG_PORT}?control=1[audio_out]"
    )

    filter_complex = ';'.join(video_filter_parts + audio_filter_parts)

    ffmpeg_cmd += ['-filter_complex', filter_complex]
    ffmpeg_cmd += ['-map', '[final_video]', '-map', '[audio_out]']

    ffmpeg_cmd += [
        '-c:v', CODEC,
        '-b:v', BW,
        '-c:a', 'aac',
        '-f', 'mpegts',
        'pipe:1'
    ]

    logger.info(f"Starting FFmpeg with command: {' '.join(ffmpeg_cmd)}")

    def generate():
        local_zmq_context = None
        local_zmq_socket = None
        process = None
        logger.info("Entering generate() function.")
        try:
            logger.info("Initializing ZeroMQ context.")
            local_zmq_context = zmq.Context()
            local_zmq_socket = local_zmq_context.socket(zmq.REQ)
            # Set a timeout for receive operations to prevent hanging
            local_zmq_socket.setsockopt(zmq.RCVTIMEO, 2000) # 2 seconds timeout for receiving

            logger.info(f"Attempting to connect ZeroMQ socket to tcp://127.0.0.1:{ZMQ_FFMPEG_PORT}")
            local_zmq_socket.connect(f"tcp://127.0.0.1:{ZMQ_FFMPEG_PORT}")
            logger.info(f"ZeroMQ socket connected to FFmpeg at tcp://127.0.0.1:{ZMQ_FFMPEG_PORT}")

            logger.info("Starting FFmpeg subprocess.")
            process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info("FFmpeg process started.")

            # Give FFmpeg a moment to initialize the ZeroMQ listener
            # Consider increasing this if connection issues persist, or rely more on ZeroMQ timeouts.
            time.sleep(1.0) # Increased sleep slightly for robustness

            # Send initial highlight command
            initial_highlight_commands = []
            for i in range(num_inputs):
                initial_highlight_commands.append(
                    f"drawbox@v{i} enable {1 if i == current_active_video_track else 0}"
                )
            logger.info("Sending initial highlight commands to FFmpeg.")
            for cmd in initial_highlight_commands:
                try:
                    local_zmq_socket.send_string(cmd)
                    response = local_zmq_socket.recv_string() # This will now respect RCVTIMEO
                    logger.info(f"Initial highlight command response for '{cmd}': {response}")
                except zmq.error.Again:
                    logger.warning(f"FFmpeg did not respond to command '{cmd}' within timeout. (zmq.error.Again)")
                except Exception as e:
                    logger.error(f"Error sending initial highlight command: {cmd}, {e}")

            logger.info("Starting to stream video chunks.")
            # Stream the video chunks
            while True:
                chunk = process.stdout.read(1024 * 16)
                if not chunk:
                    logger.info("Client disconnected or FFmpeg stream ended. Stopping FFmpeg process.")
                    break
                yield chunk

            process.wait()
            if process.returncode != 0:
                stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
                logger.error(f"FFmpeg process exited with error code {process.returncode}. Stderr: {stderr_output}")
                yield f"FFmpeg error: Process exited with code {process.returncode}. Details: {stderr_output}".encode('utf-8')
            else:
                logger.info("FFmpeg process finished successfully.")

        except FileNotFoundError:
            logger.error(f"FFmpeg executable not found. Please ensure FFmpeg is installed and in the system's PATH. Command attempted: {' '.join(ffmpeg_cmd)}")
            yield "FFmpeg not found. Please ensure it is installed and accessible.".encode('utf-8')
        except zmq.error.ZMQError as e:
            logger.error(f"ZeroMQ error during FFmpeg control: {e}", exc_info=True)
            yield f"ZeroMQ error: {e}".encode('utf-8')
        except Exception as e:
            logger.error(f"An unexpected error occurred while running FFmpeg: {e}", exc_info=True)
            yield f"An internal server error occurred: {e}".encode('utf-8')
        finally:
            logger.info("Entering finally block for FFmpeg process cleanup.")
            if process and process.poll() is None:
                logger.warning("FFmpeg process still running, terminating it in finally block.")
                process.kill()
                process.wait()
            if local_zmq_socket and not local_zmq_socket.closed:
                logger.info("Closing ZeroMQ socket for this stream.")
                local_zmq_socket.close()
            if local_zmq_context:
                local_zmq_context.term() # Terminate the context when done
                logger.info("ZeroMQ context terminated for this stream.")
            logger.info("Exiting generate() function.")

    return Response(stream_with_context(generate()), mimetype='video/MP2T')

@app.route('/switch_stream', methods=['POST'])
def switch_stream():
    """
    Endpoint to switch the active audio and video highlight for the currently streaming FFmpeg process.
    Expects 'track_index' (0-3) as a JSON payload.
    """
    global current_active_video_track

    if not request.is_json:
        return "Request must be JSON", 400

    data = request.get_json()
    track_index = data.get('track_index')

    if track_index is None or not isinstance(track_index, int) or not (0 <= track_index <= 3):
        return "Invalid 'track_index'. Must be an integer between 0 and 3.", 400

    current_active_video_track = track_index
    logger.info(f"Updated current_active_video_track to {track_index}. This will affect new streams.")

    # Explicitly indicate that this endpoint cannot control live streams without re-architecture.
    return "Stream control for active streams is not directly available with the current architecture. " \
           "This update sets the default for new streams only. To control active streams, " \
           "a mechanism to link /switch_stream requests to specific active /combine streams (e.g., using stream IDs) " \
           "and their ZeroMQ sockets would be required.", 501

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
