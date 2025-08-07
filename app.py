import subprocess
import logging
import json
import os
import requests
import time
import threading
import httpx
from flask import Flask, request, jsonify, Response, stream_with_context

app = Flask(__name__)

# --- Basic Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Environment & Global Variables ---
CONFIG_DIR = os.getenv('CONFIG_DIR', '/app/config')
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, 'roku_channels.json')
DEBUG_LOGGING_ENABLED = os.getenv('ENABLE_DEBUG_LOGGING', 'false').lower() == 'true'
ENCODING_MODE = os.getenv('ENCODING_MODE', 'proxy').lower()

# --- State Management for Tuner Pool ---
TUNERS = []
CHANNELS = []
TUNER_LOCK = threading.Lock()
ENCODER_SETTINGS = {}

# --- Core Application Logic ---

def get_encoder_options():
    """Detects available ffmpeg hardware acceleration."""
    if DEBUG_LOGGING_ENABLED: logging.info("Detecting available hardware acceleration encoders...")
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
    """Releases a locked tuner and sends a 'Home' command to the Roku."""
    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('roku_ip') == tuner_ip:
                tuner['in_use'] = False
                if DEBUG_LOGGING_ENABLED: logging.info(f"Released tuner: {tuner.get('name', tuner.get('roku_ip'))}")
                
                try:
                    home_url = f"http://{tuner_ip}:8060/keypress/Home"
                    requests.post(home_url, timeout=5)
                    if DEBUG_LOGGING_ENABLED:
                        logging.info(f"Sent 'Home' command to Roku at {tuner_ip}")
                except requests.exceptions.RequestException as e:
                    if DEBUG_LOGGING_ENABLED:
                        logging.warning(f"Failed to send 'Home' command to Roku at {tuner_ip}: {e}")
                break

# --- Streaming Functions ---

def reencode_stream_generator(encoder_url, roku_ip_to_release):
    """Generator for a more optimized ffmpeg method.
    It copies the video stream and re-encodes only the audio.
    This is much less CPU-intensive than a full re-encode."""
    try:
        command = [
            'ffmpeg',
            '-err_detect', 'ignore_err', # Ignore non-fatal errors in the input
            '-fflags', '+genpts',      # Generate new presentation timestamps
            '-i', encoder_url,
            '-c:v', 'copy',            # Copy the video stream without re-encoding
            '-c:a', 'aac',             # Re-encode the audio to AAC
            '-b:a', '128k',            # Set audio bitrate
            '-f', 'mpegts',
            '-loglevel', 'error',
            '-'
        ]
        if DEBUG_LOGGING_ENABLED:
            logging.info(f"Starting FFMPEG RE-ENCODE (Audio Only) for tuner {roku_ip_to_release}")
            logging.info(f"FFMPEG command: {' '.join(command)}")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        for chunk in iter(lambda: process.stdout.read(8192), b''):
            yield chunk

        process.wait()
        if process.returncode != 0:
            stderr_output = process.stderr.read().decode()
            logging.error(f"ffmpeg for {roku_ip_to_release} exited with error: {stderr_output}")

    finally:
        release_tuner(roku_ip_to_release)

def remux_stream_generator(encoder_url, roku_ip_to_release):
    """Generator for the low-CPU ffmpeg remuxing method."""
    try:
        command = [
            'ffmpeg',
            '-analyzeduration', '10M',
            '-probesize', '10M',
            '-err_detect', 'ignore_err',
            '-fflags', '+genpts',
            '-i', encoder_url,
            '-c', 'copy',
            '-map', '0',
            '-f', 'mpegts',
            '-loglevel', 'error',
            '-'
        ]
        if DEBUG_LOGGING_ENABLED: logging.info(f"Starting FFMPEG REMUX for tuner {roku_ip_to_release}")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for chunk in iter(lambda: process.stdout.read(8192), b''):
            yield chunk
        process.wait()
        if process.returncode != 0:
            logging.error(f"ffmpeg for {roku_ip_to_release} exited with error: {process.stderr.read().decode()}")
    finally:
        release_tuner(roku_ip_to_release)

def proxy_stream_generator(encoder_url, roku_ip_to_release):
    """Generator for the low-CPU, resilient HTTPX proxy method."""
    try:
        if DEBUG_LOGGING_ENABLED: logging.info(f"Starting HTTPX PROXY for tuner {roku_ip_to_release}")
        transport = httpx.HTTPTransport(retries=5)
        timeout = httpx.Timeout(15.0)
        headers = {"accept": "*/*", "range": "bytes=0-"}

        with httpx.Client(timeout=timeout, transport=transport, headers=headers, follow_redirects=True) as client:
            for _ in range(10):
                try:
                    with client.stream("GET", encoder_url) as r:
                        r.raise_for_status()
                        for data in r.iter_bytes():
                            yield data
                except (httpx.RequestError, httpx.HTTPStatusError) as e:
                    if DEBUG_LOGGING_ENABLED: logging.warning(f"Stream for {roku_ip_to_release} broke, retrying... Error: {e}")
                    time.sleep(1)
                else:
                    break
    except Exception as e:
        logging.error(f"Error in proxy_stream_generator for {roku_ip_to_release}: {e}")
    finally:
        release_tuner(roku_ip_to_release)


# --- Flask Routes ---

@app.route('/channels.m3u')
def generate_m3u():
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
        if DEBUG_LOGGING_ENABLED: logging.info(f"Waiting for {delay_seconds} seconds (tune_delay)...")
        time.sleep(delay_seconds)

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to tune Roku {locked_tuner['roku_ip']}: {e}")
        release_tuner(locked_tuner['roku_ip'])
        return f"Failed to tune Roku: {e}", 500

    if ENCODING_MODE == 'reencode':
        stream_generator = reencode_stream_generator(locked_tuner['encoder_url'], locked_tuner['roku_ip'])
    elif ENCODING_MODE == 'remux':
        stream_generator = remux_stream_generator(locked_tuner['encoder_url'], locked_tuner['roku_ip'])
    else: # Default to proxy
        stream_generator = proxy_stream_generator(locked_tuner['encoder_url'], locked_tuner['roku_ip'])
    
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
    return f"Roku Channels Bridge is running with {len(TUNERS)} tuners available in '{ENCODING_MODE}' mode."

# --- App Initialization ---
if __name__ != '__main__':
    load_config()
    ENCODER_SETTINGS = get_encoder_options()
