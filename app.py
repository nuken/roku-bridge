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
# NEW: Check for the debug logging environment variable
DEBUG_LOGGING_ENABLED = os.getenv('ENABLE_DEBUG_LOGGING', 'false').lower() == 'true'


# --- State Management for Tuner Pool ---
TUNERS = []
CHANNELS = []
TUNER_LOCK = threading.Lock()
ENCODER_SETTINGS = {}

# --- Core Application Logic ---

def get_encoder_options():
    """Detects available ffmpeg hardware acceleration."""
    if DEBUG_LOGGING_ENABLED:
        logging.info("Detecting available hardware acceleration encoders...")
    try:
        result = subprocess.run(['ffmpeg', '-encoders'], capture_output=True, text=True, check=True)
        available_encoders = result.stdout
        if 'h264_nvenc' in available_encoders:
            if DEBUG_LOGGING_ENABLED: logging.info("NVIDIA NVENC detected.")
            return {"codec": "h264_nvenc", "preset_args": ['-preset', 'p2'], "hwaccel_args": []}
        if 'h264_qsv' in available_encoders:
            if DEBUG_LOGGING_ENABLED: logging.info("Intel QSV detected.")
            return {"codec": "h264_qsv", "preset_args": [], "hwaccel_args": ['-hwaccel', 'qsv', '-c:v', 'h264_qsv']}
        if DEBUG_LOGGING_ENABLED: logging.info("No hardware acceleration detected. Using software encoding.")
        return {"codec": "libx264", "preset_args": ['-preset', 'superfast'], "hwaccel_args": []}
    except Exception as e:
        logging.error(f"ffmpeg detection failed: {e}. Defaulting to software encoding.")
        return {"codec": "libx264", "preset_args": ['-preset', 'superfast'], "hwaccel_args": []}

def load_config():
    """Loads tuner and channel configuration."""
    global TUNERS, CHANNELS
    if not os.path.exists(CONFIG_FILE_PATH):
        if DEBUG_LOGGING_ENABLED: logging.warning(f"Config file not found: {CONFIG_FILE_PATH}")
        return
    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            config_data = json.load(f)
        TUNERS = sorted(config_data.get('tuners', []), key=lambda x: x.get('priority', 99))
        for tuner in TUNERS:
            tuner['in_use'] = False
        CHANNELS = config_data.get('channels', [])
        if DEBUG_LOGGING_ENABLED: logging.info(f"Loaded {len(TUNERS)} tuners and {len(CHANNELS)} channels.")
    except Exception as e:
        logging.error(f"Error loading config: {e}")
        TUNERS, CHANNELS = [], []

def lock_tuner():
    """Finds and locks an available tuner."""
    with TUNER_LOCK:
        for tuner in TUNERS:
            if not tuner.get('in_use'):
                tuner['in_use'] = True
                if DEBUG_LOGGING_ENABLED: logging.info(f"Locked tuner: {tuner.get('name', tuner.get('roku_ip'))}")
                return tuner
    return None

def release_tuner(tuner_ip):
    """Releases a locked tuner."""
    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('roku_ip') == tuner_ip:
                tuner['in_use'] = False
                if DEBUG_LOGGING_ENABLED: logging.info(f"Released tuner: {tuner.get('name', tuner.get('roku_ip'))}")
                break

def reencode_stream(encoder_url, roku_ip_to_release):
    """Generator function to re-encode the stream and release the tuner."""
    try:
        command = (
            ['ffmpeg'] + ENCODER_SETTINGS['hwaccel_args'] +
            ['-i', encoder_url] +
            ['-c:v', ENCODER_SETTINGS['codec']] + ENCODER_SETTINGS['preset_args'] +
            ['-b:v', '4000k', '-c:a', 'aac', '-b:a', '128k'] +
            ['-f', 'mpegts', '-loglevel', 'error', '-']
        )
        if DEBUG_LOGGING_ENABLED: logging.info(f"Starting ffmpeg for tuner {roku_ip_to_release}: {' '.join(command)}")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for chunk in iter(lambda: process.stdout.read(8192), b''):
            yield chunk
        process.wait()
        if process.returncode != 0:
            logging.error(f"ffmpeg for {roku_ip_to_release} exited with error: {process.stderr.read().decode()}")
    finally:
        release_tuner(roku_ip_to_release)

# --- Flask Routes ---

@app.route('/channels.m3u')
def generate_m3u():
    """Generates the M3U playlist, including optional guide shift."""
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    for channel in CHANNELS:
        if all(k in channel for k in ["id", "name", "tvc_guide_stationid"]):
            stream_url = f"http://{request.host}/stream/{channel['id']}"
            
            extinf_parts = [
                f'#EXTINF:-1 channel-id="{channel["id"]}"',
                f'tvg-id="{channel["id"]}"',
                f'tvg-name="{channel["name"]}"',
                f'tvc-guide-stationid="{channel["tvc_guide_stationid"]}"'
            ]
            
            if "guide_shift" in channel:
                extinf_parts.append(f'tvc-guide-shift="{channel["guide_shift"]}"')
            
            extinf = ' '.join(extinf_parts) + f',{channel["name"]}'
            
            m3u_content.append(extinf)
            m3u_content.append(stream_url)
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    """Locks a tuner, tunes the Roku with optional delay, and starts the stream."""
    locked_tuner = lock_tuner()
    if not locked_tuner:
        if DEBUG_LOGGING_ENABLED: logging.warning("Stream request failed: All tuners are in use.")
        return "All tuners are currently in use.", 503

    channel_data = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel_data:
        release_tuner(locked_tuner['roku_ip'])
        return "Channel not found.", 404

    try:
        roku_app_id = channel_data["roku_app_id"]
        content_id = channel_data["deep_link_content_id"]
        media_type = channel_data["media_type"]
        roku_tune_url = f"http://{locked_tuner['roku_ip']}:8060/launch/{roku_app_id}?contentId={content_id}&mediaType={media_type}"
        requests.post(roku_tune_url, timeout=10).raise_for_status()

        if channel_data.get("needs_select_keypress", False):
            time.sleep(1)
            keypress_url = f"http://{locked_tuner['roku_ip']}:8060/keypress/Select"
            requests.post(keypress_url, timeout=5).raise_for_status()
            if DEBUG_LOGGING_ENABLED: logging.info(f"Sent 'Select' keypress to {locked_tuner['roku_ip']}")

        delay_seconds = channel_data.get("tune_delay", 3)
        if DEBUG_LOGGING_ENABLED: logging.info(f"Waiting for {delay_seconds} seconds (tune_delay) before starting stream...")
        time.sleep(delay_seconds)

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to tune Roku {locked_tuner['roku_ip']}: {e}")
        release_tuner(locked_tuner['roku_ip'])
        return f"Failed to tune Roku: {e}", 500

    stream_generator = reencode_stream(locked_tuner['encoder_url'], locked_tuner['roku_ip'])
    return Response(stream_with_context(stream_generator), mimetype='video/mpeg')

@app.route('/upload_config', methods=['POST'])
def upload_config():
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
    load_config()
    ENCODER_SETTINGS = get_encoder_options()
