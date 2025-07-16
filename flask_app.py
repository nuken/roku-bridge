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
zmq_context = zmq.Context()
zmq_socket = None # This will be the REQ socket to send commands to FFmpeg

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
    global zmq_socket, current_active_video_track

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

    # Corrected azmq filter syntax for control option
    audio_filter_parts.append(
        f"[mixed_audio]azmq=bind_address='tcp://127.0.0.1:{ZMQ_FFMPEG_PORT}':control=1[audio_out]"
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
        process = None
        try:
            # Create a REQ socket for sending commands to FFmpeg
            zmq_socket = zmq_context.socket(zmq.REQ)
            zmq_socket.connect(f"tcp://127.0.0.1:{ZMQ_FFMPEG_PORT}")
            logger.info(f"ZeroMQ socket connected to FFmpeg at tcp://127.0.0.1:{ZMQ_FFMPEG_PORT}")

            # Start the FFmpeg subprocess, capturing stdout and stderr
            process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info("FFmpeg process started.")

            # Give FFmpeg a moment to initialize the ZeroMQ listener
            time.sleep(0.5) # A small delay, adjust if needed

            # Send initial highlight command to ensure it matches current_active_video_track
            initial_highlight_commands = []
            for i in range(num_inputs):
                # Ensure each drawbox filter is referenced by its unique label 'v0', 'v1', etc.
                # The command format is 'filter_label option_name value'
                initial_highlight_commands.append(
                    f"drawbox@v{i} enable {1 if i == current_active_video_track else 0}"
                )
            for cmd in initial_highlight_commands:
                try:
                    zmq_socket.send_string(cmd)
                    # For initial commands, using NOBLOCK might be too aggressive if FFmpeg is still very busy.
                    # A small delay and then recv_string without NOBLOCK is safer, or catch Again.
                    # Given the delay above, a blocking recv_string should now work.
                    response = zmq_socket.recv_string()
                    logger.info(f"Initial highlight command response for '{cmd}': {response}")
                except zmq.error.Again:
                    logger.warning(f"FFmpeg not ready to receive command (again): {cmd}")
                except Exception as e:
                    logger.error(f"Error sending initial highlight command: {cmd}, {e}")

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
            logger.error(f"ZeroMQ error during FFmpeg control: {e}")
            yield f"ZeroMQ error: {e}".encode('utf-8')
        except Exception as e:
            logger.error(f"An unexpected error occurred while running FFmpeg: {e}", exc_info=True)
            yield f"An internal server error occurred: {e}".encode('utf-8')
        finally:
            if process and process.poll() is None:
                logger.warning("FFmpeg process still running, terminating it in finally block.")
                process.kill()
                process.wait()
            if zmq_socket and not zmq_socket.closed:
                logger.info("Closing ZeroMQ socket.")
                zmq_socket.close()

    return Response(stream_with_context(generate()), mimetype='video/MP2T')

@app.route('/switch_stream', methods=['POST'])
def switch_stream():
    """
    Endpoint to switch the active audio and video highlight for the currently streaming FFmpeg process.
    Expects 'track_index' (0-3) as a JSON payload.
    """
    global zmq_socket, current_active_video_track

    if not request.is_json:
        return "Request must be JSON", 400

    data = request.get_json()
    track_index = data.get('track_index')

    if track_index is None or not isinstance(track_index, int) or not (0 <= track_index <= 3):
        return "Invalid 'track_index'. Must be an integer between 0 and 3.", 400

    current_active_video_track = track_index

    if not zmq_socket or zmq_socket.closed:
        logger.warning("ZeroMQ socket to FFmpeg is not active. Is an FFmpeg stream running?")
        return "FFmpeg stream not active or ZeroMQ socket not initialized.", 503

    try:
        commands = []
        for i in range(4):
            commands.append(f"volume@a{i} enable {1 if i == track_index else 0}")
            commands.append(f"drawbox@v{i} enable {1 if i == track_index else 0}")

        for cmd in commands:
            logger.info(f"Sending FFmpeg command: {cmd}")
            zmq_socket.send_string(cmd)
            response = zmq_socket.recv_string()
            logger.info(f"FFmpeg response for '{cmd}': {response}")
            if response != "Success":
                return f"Failed to send FFmpeg command: {cmd}. Response: {response}", 500

        return f"Stream switched to track {track_index} with highlight.", 200

    except zmq.error.ZMQError as e:
        logger.error(f"ZeroMQ communication error: {e}", exc_info=True)
        return f"ZeroMQ communication error: {e}", 500
    except Exception as e:
        logger.error(f"An unexpected error occurred during stream switch: {e}", exc_info=True)
        return f"An internal server error occurred: {e}", 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
