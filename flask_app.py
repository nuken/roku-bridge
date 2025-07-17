print("--- flask_app.py is being loaded ---")
import flask
from flask import Flask, Response, request, stream_with_context
import subprocess
import os
import logging
import threading
import zmq
import time
import uuid # Added for unique stream IDs

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configurable constants
CDVR_HOST = os.getenv("CDVR_HOST", "192.168.51.84")
CDVR_PORT = int(os.getenv("CDVR_PORT", "8089"))
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 29.97
CODEC = os.getenv("CODEC", "libx264")
BW = '5120k'

# Global store for active streams
# Each entry: {
#   'process': subprocess.Popen,
#   'video_zmq_socket': zmq.Socket,
#   'audio_zmq_sockets': {0: zmq.Socket, 1: zmq.Socket, ...},
#   'video_zmq_port': int, # To remember which port was used for this stream's video control
#   'audio_zmq_ports': {0: int, 1: int, ...}, # To remember which ports were used for this stream's audio control
#   'channel_count': int # To know how many tracks there are
# }
active_streams_info = {}
active_streams_lock = threading.Lock() # Protect access to active_streams_info

# ZeroMQ base ports for dynamic allocation
# In a production environment, you might need a more robust port management system
# or rely on ZMQ to pick ephemeral ports if possible with bind_address.
ZMQ_PORT_RANGE_START = 5500 # Starting port for ZMQ
ZMQ_PORTS_PER_STREAM = 5 # 1 for video, up to 4 for audio (adjust if max channels > 4)

def get_available_zmq_ports(num_channels):
    """Finds a block of available ZMQ ports for a new stream."""
    global ZMQ_PORT_RANGE_START
    with active_streams_lock:
        video_port = ZMQ_PORT_RANGE_START
        audio_ports = {i: ZMQ_PORT_RANGE_START + 1 + i for i in range(num_channels)}
        # Increment ZMQ_PORT_RANGE_START for the next stream
        ZMQ_PORT_RANGE_START += (1 + num_channels)
        return video_port, audio_ports

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

    # Generate a unique ID for this stream
    stream_id = uuid.uuid4().hex
    video_zmq_port, audio_zmq_ports = get_available_zmq_ports(num_inputs)

    ffmpeg_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info']

    for url in urls:
        ffmpeg_cmd += ['-i', url]

    # --- Video Filtergraph ---
    video_filter_parts = []
    video_input_labels = []

    # The initial_active_video_track for this new stream (can be changed later via /switch_stream)
    initial_active_track_idx = 0 # Default to first track

    for i in range(num_inputs):
        # Give each drawbox a unique ID for ZMQ control
        video_filter_parts.append(
            f'[{i}:v]fps={TARGET_FPS},scale={TARGET_WIDTH}:{TARGET_HEIGHT},'
            f'drawbox=x=0:y=0:w=iw:h=ih:color=red@0.8:thickness=5:enable=\'eq(1,{1 if i == initial_active_track_idx else 0})\'@drawbox_v{i}[v{i}]'
        )
        video_input_labels.append(f'[v{i}]')

    layout_map = {
        1: "[v0]xstack=inputs=1:layout=0_0[final_video_xstack]", # Renamed to avoid conflict with final_video_zmq
        2: "[v0][v1]xstack=inputs=2:layout=0_0|w0_0[final_video_xstack]",
        3: "[v0][v1][v2]xstack=inputs=3:layout=0_0|w0_0|0_h0[final_video_xstack]",
        4: "[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[final_video_xstack]"
    }
    try:
        video_filter_parts.append(layout_map[num_inputs])
    except KeyError:
        logger.error(f"Unsupported number of inputs for xstack layout: {num_inputs}")
        return f"Unsupported number of channels: {num_inputs}. Max 4 channels allowed.", 500

    # Add a ZMQ filter to the end of the video filtergraph for control
    video_filter_parts.append(
        f"[final_video_xstack]zmq=bind_address=tcp://127.0.0.1:{video_zmq_port}?control=1@video_control[final_video]"
    )


    # --- Audio Filtergraph (for dynamic switching) ---
    audio_filter_parts = []
    audio_input_labels_for_amix = [] # These will be the outputs of individual azmq filters

    for i in range(num_inputs):
        # Give each volume filter a unique ID and put azmq after it
        audio_filter_parts.append(
            f'[{i}:a]aformat=channel_layouts=stereo,'
            f'volume=enable=\'eq(1,{1 if i == initial_active_track_idx else 0})\':eval=frame@volume_a{i},'
            f'azmq=bind_address=tcp://127.0.0.1:{audio_zmq_ports[i]}?control=1@audio_control_{i}[a_out_{i}]'
        )
        audio_input_labels_for_amix.append(f'[a_out_{i}]')

    audio_filter_parts.append(
        f"{''.join(audio_input_labels_for_amix)}amix=inputs={num_inputs}:dropout_transition=0:duration=first[mixed_audio]"
    )

    filter_complex = ';'.join(video_filter_parts + audio_filter_parts)

    ffmpeg_cmd += ['-filter_complex', filter_complex]
    ffmpeg_cmd += ['-map', '[final_video]', '-map', '[mixed_audio]'] # map to the final labels

    ffmpeg_cmd += [
        '-c:v', CODEC,
        '-b:v', BW,
        '-c:a', 'aac',
        '-f', 'mpegts',
        'pipe:1'
    ]

    logger.info(f"[{stream_id}] Starting FFmpeg with command: {' '.join(ffmpeg_cmd)}")

    def generate():
        local_zmq_context = None
        video_zmq_socket = None
        audio_zmq_sockets_map = {} # map track_idx to socket
        process = None
        logger.info(f"[{stream_id}] Entering generate() function.")
        try:
            logger.info(f"[{stream_id}] Initializing ZeroMQ context.")
            local_zmq_context = zmq.Context()

            # Setup video control socket
            video_zmq_socket = local_zmq_context.socket(zmq.REQ)
            video_zmq_socket.setsockopt(zmq.RCVTIMEO, 2000) # 2 seconds timeout for receiving
            logger.info(f"[{stream_id}] Attempting to connect video ZMQ socket to tcp://127.0.0.1:{video_zmq_port}")
            video_zmq_socket.connect(f"tcp://127.0.0.1:{video_zmq_port}")
            logger.info(f"[{stream_id}] Video ZMQ socket connected.")

            # Setup audio control sockets
            for i in range(num_inputs):
                sock = local_zmq_context.socket(zmq.REQ)
                sock.setsockopt(zmq.RCVTIMEO, 2000)
                port = audio_zmq_ports[i]
                logger.info(f"[{stream_id}] Attempting to connect audio ZMQ socket for track {i} to tcp://127.0.0.1:{port}")
                sock.connect(f"tcp://127.0.0.1:{port}")
                audio_zmq_sockets_map[i] = sock
                logger.info(f"[{stream_id}] Audio ZMQ socket for track {i} connected.")

            logger.info(f"[{stream_id}] Starting FFmpeg subprocess.")
            process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            logger.info(f"[{stream_id}] FFmpeg process started with PID: {process.pid}")

            # Store stream info globally
            with active_streams_lock:
                active_streams_info[stream_id] = {
                    'process': process,
                    'video_zmq_socket': video_zmq_socket,
                    'audio_zmq_sockets': audio_zmq_sockets_map,
                    'video_zmq_port': video_zmq_port,
                    'audio_zmq_ports': audio_zmq_ports,
                    'channel_count': num_inputs
                }
            logger.info(f"[{stream_id}] Stream info stored in active_streams_info.")

            # Give FFmpeg a moment to initialize the ZeroMQ listener
            time.sleep(1.0)

            # Send initial highlight commands (already handled by enable in filtergraph, but good to re-send for robustness)
            # and verify ZMQ communication
            initial_highlight_commands = []
            for i in range(num_inputs):
                initial_highlight_commands.append(
                    f"drawbox_v{i} enable {1 if i == initial_active_track_idx else 0}"
                )
            logger.info(f"[{stream_id}] Sending initial highlight commands to FFmpeg.")
            for cmd in initial_highlight_commands:
                try:
                    video_zmq_socket.send_string(cmd)
                    response = video_zmq_socket.recv_string()
                    logger.info(f"[{stream_id}] Initial highlight command response for '{cmd}': {response}")
                except zmq.error.Again:
                    logger.warning(f"[{stream_id}] FFmpeg did not respond to command '{cmd}' within timeout. (zmq.error.Again)")
                except Exception as e:
                    logger.error(f"[{stream_id}] Error sending initial highlight command: {cmd}, {e}")

            logger.info(f"[{stream_id}] Starting to stream video chunks.")
            # Stream the video chunks
            while True:
                chunk = process.stdout.read(1024 * 16)
                if not chunk:
                    logger.info(f"[{stream_id}] Client disconnected or FFmpeg stream ended. Stopping FFmpeg process.")
                    break
                yield chunk

            process.wait()
            if process.returncode != 0:
                stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
                logger.error(f"[{stream_id}] FFmpeg process exited with error code {process.returncode}. Stderr: {stderr_output}")
                yield f"FFmpeg error: Process exited with code {process.returncode}. Details: {stderr_output}".encode('utf-8')
            else:
                logger.info(f"[{stream_id}] FFmpeg process finished successfully.")

        except FileNotFoundError:
            logger.error(f"[{stream_id}] FFmpeg executable not found. Please ensure FFmpeg is installed and in the system's PATH. Command attempted: {' '.join(ffmpeg_cmd)}")
            yield "FFmpeg not found. Please ensure it is installed and accessible.".encode('utf-8')
        except zmq.error.ZMQError as e:
            logger.error(f"[{stream_id}] ZeroMQ error during FFmpeg control: {e}", exc_info=True)
            yield f"ZeroMQ error: {e}".encode('utf-8')
        except Exception as e:
            logger.error(f"[{stream_id}] An unexpected error occurred while running FFmpeg: {e}", exc_info=True)
            yield f"An internal server error occurred: {e}".encode('utf-8')
        finally:
            logger.info(f"[{stream_id}] Entering finally block for FFmpeg process cleanup.")
            # Remove stream info from global store
            with active_streams_lock:
                if stream_id in active_streams_info:
                    del active_streams_info[stream_id]
                    logger.info(f"[{stream_id}] Removed from active_streams_info.")

            if process and process.poll() is None:
                logger.warning(f"[{stream_id}] FFmpeg process still running, terminating it in finally block.")
                process.kill() # or os.killpg(os.getpgid(process.pid), 9) for brutal kill
                process.wait()
            if video_zmq_socket and not video_zmq_socket.closed:
                logger.info(f"[{stream_id}] Closing video ZeroMQ socket.")
                video_zmq_socket.close()
            for sock in audio_zmq_sockets_map.values():
                if sock and not sock.closed:
                    logger.info(f"[{stream_id}] Closing an audio ZeroMQ socket.")
                    sock.close()
            if local_zmq_context:
                local_zmq_context.term() # Terminate the context when all sockets are closed
                logger.info(f"[{stream_id}] ZeroMQ context terminated.")
            logger.info(f"[{stream_id}] Exiting generate() function.")

    response = Response(stream_with_context(generate()), mimetype='video/MP2T')
    response.headers['X-Stream-ID'] = stream_id # Provide stream ID in response header
    return response

@app.route('/switch_stream', methods=['POST'])
def switch_stream():
    """
    Endpoint to switch the active audio track and video highlight for a specific currently streaming FFmpeg process.
    Expects 'stream_id' and 'track_index' (0-3) as a JSON payload.
    """
    if not request.is_json:
        return "Request must be JSON", 400

    data = request.get_json()
    stream_id = data.get('stream_id')
    track_index = data.get('track_index')

    if not stream_id:
        return "Missing 'stream_id' in JSON payload.", 400
    if track_index is None or not isinstance(track_index, int) or not (0 <= track_index <= 3):
        return "Invalid 'track_index'. Must be an integer between 0 and 3.", 400

    with active_streams_lock:
        stream_info = active_streams_info.get(stream_id)

    if not stream_info:
        logger.warning(f"Received switch request for unknown stream ID: {stream_id}")
        return jsonify({"error": f"Stream with ID '{stream_id}' not found or no longer active."}), 404

    video_zmq_socket = stream_info['video_zmq_socket']
    audio_zmq_sockets_map = stream_info['audio_zmq_sockets']
    num_channels = stream_info['channel_count']

    if not (0 <= track_index < num_channels):
        return jsonify({"error": f"Track index {track_index} out of bounds for stream with {num_channels} channels."}), 400

    logger.info(f"[{stream_id}] Switching to audio/video track: {track_index}")

    # --- Send Video Highlight Commands ---
    video_commands = []
    for i in range(num_channels):
        # Enable drawbox for the selected track, disable for others
        enable_val = 1 if i == track_index else 0
        video_commands.append(f"drawbox_v{i} enable {enable_val}")

    success_video = True
    for cmd in video_commands:
        try:
            video_zmq_socket.send_string(cmd)
            response = video_zmq_socket.recv_string()
            logger.info(f"[{stream_id}] Video ZMQ response for '{cmd}': {response}")
            if not response.startswith('200'): # ZMQ filter returns 200 OK on success
                success_video = False
        except zmq.error.Again:
            logger.warning(f"[{stream_id}] Video ZMQ timeout for command: '{cmd}'.")
            success_video = False
        except Exception as e:
            logger.error(f"[{stream_id}] Error sending video ZMQ command '{cmd}': {e}")
            success_video = False

    # --- Send Audio Volume Commands ---
    success_audio = True
    for i in range(num_channels):
        volume_val = 1 if i == track_index else 0
        audio_cmd = f"volume_a{i} volume {volume_val}"
        try:
            audio_zmq_socket = audio_zmq_sockets_map.get(i)
            if audio_zmq_socket:
                audio_zmq_socket.send_string(audio_cmd)
                response = audio_zmq_socket.recv_string()
                logger.info(f"[{stream_id}] Audio ZMQ response for '{audio_cmd}': {response}")
                if not response.startswith('200'):
                    success_audio = False
            else:
                logger.error(f"[{stream_id}] Audio ZMQ socket for track {i} not found.")
                success_audio = False
        except zmq.error.Again:
            logger.warning(f"[{stream_id}] Audio ZMQ timeout for command: '{audio_cmd}'.")
            success_audio = False
        except Exception as e:
            logger.error(f"[{stream_id}] Error sending audio ZMQ command '{audio_cmd}': {e}")
            success_audio = False

    if success_video and success_audio:
        return jsonify({"status": f"Switched stream {stream_id} to track {track_index}.", "track_index": track_index}), 200
    else:
        return jsonify({"error": f"Failed to switch stream {stream_id} to track {track_index}. See logs for details."}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
