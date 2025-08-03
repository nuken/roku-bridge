import subprocess
import logging
import json
import os
import requests
import time
import threading
from flask import Flask, request, jsonify, Response, stream_with_context

app = Flask(__name__)

# --- Basic Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Environment & Global Variables ---
CONFIG_DIR = os.getenv('CONFIG_DIR', '/app/config')
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, 'roku_channels.json')

# --- State Management for Tuner Pool ---
# These will be populated by load_config()
TUNERS = []
CHANNELS = []
# A lock to ensure thread-safe access to the TUNERS list
TUNER_LOCK = threading.Lock()
# This global dictionary will store the best ffmpeg options found on startup
ENCODER_SETTINGS = {}

# --- Core Application Logic ---

def get_encoder_options():
    """
    Detects available ffmpeg hardware acceleration and returns the best options.
    This function runs only once on application startup.
    """
    logging.info("Detecting available hardware acceleration encoders...")
    try:
        result = subprocess.run(['ffmpeg', '-encoders'], capture_output=True, text=True, check=True)
        available_encoders = result.stdout
        if 'h264_nvenc' in available_encoders:
            logging.info("NVIDIA NVENC detected. Using hardware acceleration.")
            return {"codec": "h264_nvenc", "preset_args": ['-preset', 'p2'], "hwaccel_args": []}
        if 'h264_qsv' in available_encoders:
            logging.info("Intel QSV detected. Using hardware acceleration.")
            return {"codec": "h264_qsv", "preset_args": [], "hwaccel_args": ['-hwaccel', 'qsv', '-c:v', 'h264_qsv']}
        logging.info("No hardware acceleration detected. Falling back to efficient software encoding.")
        return {"codec": "libx264", "preset_args": ['-preset', 'superfast'], "hwaccel_args": []}
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logging.error(f"ffmpeg not found or error during detection: {e}. Defaulting to software encoding.")
        return {"codec": "libx264", "preset_args": ['-preset', 'superfast'], "hwaccel_args": []}

def load_config():
    """Loads the tuner and channel configuration from the JSON file."""
    global TUNERS, CHANNELS
    if not os.path.exists(CONFIG_FILE_PATH):
        logging.warning(f"No config file found at {CONFIG_FILE_PATH}.")
        return

    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            config_data = json.load(f)
        
        # Load and sort tuners by priority
        loaded_tuners = config_data.get('tuners', [])
        TUNERS = sorted(loaded_tuners, key=lambda x: x.get('priority', 99))
        for tuner in TUNERS:
            tuner['in_use'] = False # Initialize all tuners as not in use
        
        CHANNELS = config_data.get('channels', [])
        logging.info(f"Loaded {len(TUNERS)} tuners and {len(CHANNELS)} channels.")

    except (json.JSONDecodeError, Exception) as e:
        logging.error(f"Error loading or parsing config file {CONFIG_FILE_PATH}: {e}")
        TUNERS, CHANNELS = [], []

def lock_tuner():
    """Finds and locks the highest-priority available tuner."""
    with TUNER_LOCK:
        for tuner in TUNERS:
            if not tuner.get('in_use'):
                tuner['in_use'] = True
                logging.info(f"Locked tuner: {tuner.get('name', tuner.get('roku_ip'))}")
                return tuner
    return None # No tuners available

def release_tuner(tuner_ip):
    """Releases a tuner, making it available for other streams."""
    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('roku_ip') == tuner_ip:
                tuner['in_use'] = False
                logging.info(f"Released tuner: {tuner.get('name', tuner.get('roku_ip'))}")
                break

def reencode_stream(encoder_url, roku_ip_to_release):
    """
    Generator function that re-encodes the stream and ensures the tuner is released.
    """
    try:
        command = (
            ['ffmpeg'] + ENCODER_SETTINGS['hwaccel_args'] +
            ['-i', encoder_url] +
            ['-c:v', ENCODER_SETTINGS['codec']] + ENCODER_SETTINGS['preset_args'] +
            ['-b:v', '4000k', '-c:a', 'aac', '-b:a', '128k'] +
            ['-f', 'mpegts', '-loglevel', 'error', '-']
        )
        logging.info(f"Starting ffmpeg for tuner {roku_ip_to_release}: {' '.join(command)}")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        for chunk in iter(lambda: process.stdout.read(8192), b''):
            yield chunk
            
        process.wait()
        if process.returncode != 0:
            logging.error(f"ffmpeg for {roku_ip_to_release} exited with error: {process.stderr.read().decode()}")
    finally:
        # This is crucial: it runs whether the stream finishes or the client disconnects.
        release_tuner(roku_ip_to_release)

# --- Flask Routes ---

@app.route('/channels.m3u')
def generate_m3u():
    """Generates the M3U playlist, including the max stream count."""
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    for channel in CHANNELS:
        if all(k in channel for k in ["id", "name", "tvc_guide_stationid"]):
            stream_url = f"http://{request.host}/stream/{channel['id']}"
            extinf = (f'#EXTINF:-1 channel-id="{channel["id"]}" '
                      f'tvg-id="{channel["id"]}" '
                      f'tvg-name="{channel["name"]}" '
                      f'tvc-guide-stationid="{channel["tvc_guide_stationid"]}",{channel["name"]}')
            m3u_content.append(extinf)
            m3u_content.append(stream_url)
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    """Locks a tuner, tunes the Roku, and starts the stream."""
    locked_tuner = lock_tuner()
    if not locked_tuner:
        logging.warning("Stream request failed: All tuners are currently in use.")
        return "All tuners are currently in use.", 503

    channel_data = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel_data:
        release_tuner(locked_tuner['roku_ip'])
        return "Channel not found in configuration.", 404

    try:
        roku_app_id = channel_data["roku_app_id"]
        content_id = channel_data["deep_link_content_id"]
        media_type = channel_data["media_type"]
        roku_tune_url = f"http://{locked_tuner['roku_ip']}:8060/launch/{roku_app_id}?contentId={content_id}&mediaType={media_type}"
        
        requests.post(roku_tune_url, timeout=10).raise_for_status()
        time.sleep(3) # Give Roku time to tune

        if roku_app_id == "20197": # Special handling for YouTube TV
            keypress_url = f"http://{locked_tuner['roku_ip']}:8060/keypress/Select"
            requests.post(keypress_url, timeout=5).raise_for_status()
            time.sleep(1)

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to tune Roku device {locked_tuner['roku_ip']}: {e}")
        release_tuner(locked_tuner['roku_ip']) # Release tuner on failure
        return f"Failed to tune Roku: {e}", 500

    # Start the streaming generator, passing the necessary info to release the tuner later
    stream_generator = reencode_stream(locked_tuner['encoder_url'], locked_tuner['roku_ip'])
    return Response(stream_with_context(stream_generator), mimetype='video/mpeg')

@app.route('/upload_config', methods=['POST'])
def upload_config():
    """Allows uploading a new JSON configuration file."""
    # ... (function content is unchanged)
    if 'file' not in request.files: return "No file part", 400
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.json'): return "Invalid file", 400
    try:
        new_config_data = json.load(file.stream)
        with open(CONFIG_FILE_PATH, 'w') as f:
            json.dump(new_config_data, f, indent=2)
        load_config()
        return "Configuration updated successfully", 200
    except Exception as e:
        return f"Error processing config file: {e}", 400

@app.route('/')
def index():
    return f"Roku Channels Bridge is running with {len(TUNERS)} tuners available."

# --- App Initialization ---
if __name__ != '__main__':
    # This block runs when the app is started by Gunicorn in Docker
    load_config()
    ENCODER_SETTINGS = get_encoder_options()

