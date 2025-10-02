import subprocess
import logging
from logging import StreamHandler
import json
import os
import requests
import time
import threading
import httpx
import urllib.parse
import signal
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, stream_with_context, render_template

# --- Import Plugin System ---
from plugins import discovered_plugins

app = Flask(__name__)

# --- Application Version ---
APP_VERSION = "4.0-stream" # Updated Version

# --- Disable caching ---
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.config['TEMPLATES_AUTO_RELOAD'] = True

# --- Global Log Buffer ---
LOG_BUFFER_SIZE = 1000
log_buffer = deque(maxlen=LOG_BUFFER_SIZE)

class DequeLogHandler(StreamHandler):
    def __init__(self, target_deque):
        super().__init__()
        self.target_deque = target_deque
    def emit(self, record):
        try:
            msg = self.format(record)
            self.target_deque.append(msg)
        except Exception: self.handleError(record)

# --- Basic Configuration ---
log_format = '%(asctime)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=log_format)
root_logger = logging.getLogger()
deque_handler = DequeLogHandler(log_buffer)
formatter = logging.Formatter(log_format)
deque_handler.setFormatter(formatter)
root_logger.addHandler(deque_handler)

# --- Environment & Global Variables ---
CONFIG_DIR = os.getenv('CONFIG_DIR', '/app/config')
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, 'roku_channels.json')
DEBUG_LOGGING_ENABLED = os.getenv('ENABLE_DEBUG_LOGGING', 'false').lower() == 'true'
ENCODING_MODE = os.getenv('ENCODING_MODE', 'proxy').lower()
AUDIO_BITRATE = os.getenv('AUDIO_BITRATE', '128k')
SILENT_TS_PACKET = b'\x47\x40\x11\x10\x00\x02\xb0\x0d\x00\x01\xc1\x00\x00' + b'\xff' * 175

def get_audio_channels():
    channels_input = os.getenv('AUDIO_CHANNELS', '2').lower()
    return '6' if channels_input == "5.1" else '8' if channels_input == "7.1" else channels_input
AUDIO_CHANNELS = get_audio_channels()

# --- State Management ---
TUNERS, CHANNELS, EPG_CHANNELS, ONDEMAND_APPS = [], [], [], []
TUNER_LOCK = threading.Lock()
KEEP_ALIVE_TASKS = {}
PREVIEW_SESSION = {'tuner': None, 'active': False, 'committed': False} # For the new pre-tune feature
SESSION_LOCK = threading.Lock() # To manage the preview session safely

roku_session = requests.Session()
roku_session.timeout = 3
executor = ThreadPoolExecutor(max_workers=4)

# --- Core Application Logic ---

def load_config():
    global TUNERS, CHANNELS, EPG_CHANNELS, ONDEMAND_APPS
    if not os.path.exists(CONFIG_FILE_PATH):
        logging.warning(f"Config file not found at {CONFIG_FILE_PATH}. Creating default.")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w') as f:
                json.dump({"tuners": [], "channels": [], "epg_channels": [], "ondemand_apps": []}, f, indent=2)
        except Exception as e:
            logging.error(f"Could not create default config: {e}")
    try:
        with open(CONFIG_FILE_PATH, 'r') as f: config_data = json.load(f) or {}
        TUNERS = sorted(config_data.get('tuners', []), key=lambda x: x.get('priority', 99))
        for tuner in TUNERS: tuner['in_use'] = False
        CHANNELS = config_data.get('channels', [])
        EPG_CHANNELS = config_data.get('epg_channels', [])
        ONDEMAND_APPS = config_data.get('ondemand_apps', []) # Load new on-demand apps
        if DEBUG_LOGGING_ENABLED:
            logging.info(f"Loaded {len(TUNERS)} tuners, {len(CHANNELS)} Gracenote, {len(EPG_CHANNELS)} EPG channels, {len(ONDEMAND_APPS)} On-Demand apps.")
    except Exception as e:
        logging.error(f"Error loading config: {e}")

def lock_tuner():
    with TUNER_LOCK:
        for tuner in TUNERS:
            if not tuner.get('in_use'):
                tuner['in_use'] = True
                if DEBUG_LOGGING_ENABLED: logging.info(f"Locked tuner: {tuner.get('name')}")
                return tuner
    return None

def release_tuner(tuner_ip):
    if tuner_ip in KEEP_ALIVE_TASKS:
        thread, stop_event = KEEP_ALIVE_TASKS.pop(tuner_ip)
        stop_event.set()
        thread.join(timeout=5)
    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('roku_ip') == tuner_ip:
                if tuner['in_use']:
                    tuner['in_use'] = False
                    if DEBUG_LOGGING_ENABLED: logging.info(f"Released tuner: {tuner.get('name')}")
                    try: roku_session.post(f"http://{tuner_ip}:8060/keypress/Home")
                    except requests.exceptions.RequestException: pass
                break

def send_key_sequence(device_ip, keys):
    # (Existing function remains unchanged)
    ...

def keep_alive_sender(roku_ip, key_string, interval_minutes, stop_event):
    # (Existing function remains unchanged)
    ...

def execute_tuning_in_background(roku_ip, channel_data):
    # (Existing function remains unchanged)
    ...

def stream_generator(encoder_url, roku_ip_to_release, mode='proxy', blank_duration=0):
    # (Existing function remains unchanged)
    ...

# --- New Pre-Tune Session Management ---

def start_preview_session():
    with SESSION_LOCK:
        if PREVIEW_SESSION['active']:
            return {"status": "error", "message": "A preview session is already active."}

        tuner = lock_tuner()
        if not tuner:
            return {"status": "error", "message": "All tuners are in use."}

        PREVIEW_SESSION.update({'tuner': tuner, 'active': True, 'committed': False})
        logging.info(f"Started preview session on tuner {tuner['name']}")
        return {"status": "success", "tuner_name": tuner['name'], "roku_ip": tuner['roku_ip'], "encoder_url": tuner['encoder_url']}

def stop_preview_session():
    with SESSION_LOCK:
        if PREVIEW_SESSION['active'] and not PREVIEW_SESSION['committed']:
            tuner = PREVIEW_SESSION['tuner']
            if tuner:
                logging.info(f"Stopping and releasing unused preview session on tuner {tuner['name']}")
                release_tuner(tuner['roku_ip'])
        PREVIEW_SESSION.update({'tuner': None, 'active': False, 'committed': False})

def commit_preview_session():
    with SESSION_LOCK:
        if not PREVIEW_SESSION['active'] or not PREVIEW_SESSION['tuner']:
            return {"status": "error", "message": "No active preview session to commit."}
        PREVIEW_SESSION['committed'] = True
        logging.info(f"Committed preview session for tuner {PREVIEW_SESSION['tuner']['name']}. It is now locked for Channels DVR.")
        return {"status": "success", "message": "Stream is now ready for Channels DVR."}

# --- Main Flask Routes ---

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    # (Existing route remains unchanged)
    ...

# --- NEW: On-Demand Streaming Endpoint ---
@app.route('/stream/ondemand_stream')
def stream_ondemand():
    """Waits for a committed pre-tuned stream and serves it."""
    with SESSION_LOCK:
        if not PREVIEW_SESSION['committed'] or not PREVIEW_SESSION['tuner']:
            return "No pre-tuned stream is ready. Please select content from the Pre-Tune page.", 404

        tuner = PREVIEW_SESSION['tuner']
        # Reset the session for the next user
        PREVIEW_SESSION.update({'tuner': None, 'active': False, 'committed': False})

    logging.info(f"Channels DVR connected to committed stream from tuner {tuner['name']}")
    tuner_mode = tuner.get('encoding_mode', ENCODING_MODE)
    generator = stream_generator(tuner['encoder_url'], tuner['roku_ip'], tuner_mode)
    return Response(stream_with_context(generator), mimetype='video/mpeg')

# --- M3U Routes ---

def generate_m3u_from_channels(channel_list):
    # (Existing function remains unchanged)
    ...

@app.route('/channels.m3u')
def generate_gracenote_m3u():
    # (Existing function remains unchanged)
    ...

@app.route('/epg_channels.m3u')
def generate_epg_m3u():
    # (Existing function remains unchanged)
    ...

# --- NEW: On-Demand M3U ---
@app.route('/ondemand.m3u')
def generate_ondemand_m3u():
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    stream_url = f"http://{request.host}/stream/ondemand_stream"
    extinf_line = f'#EXTINF:-1 channel-id="ondemand_viewer" tvg-name="On-Demand Stream",On-Demand Stream'
    m3u_content.extend([extinf_line, stream_url])
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

# --- UI and API Routes ---

@app.route('/')
def index():
    return f"Roku Channels Bridge is running. <a href='/status'>View Status</a> | <a href='/remote'>Go to Remote</a> | <a href='/preview'>Live TV Preview</a> | <a href='/pretune'>On-Demand Pre-Tune</a>"

@app.route('/remote')
def remote_control():
    # (Existing route remains unchanged)
    ...

@app.route('/preview')
def preview():
    # (Existing route remains unchanged)
    ...

# --- NEW: Pre-Tune Page Route ---
@app.route('/pretune')
def pretune_page():
    """Renders the new pre-tuning page."""
    return render_template('pretune.html', ondemand_apps=ONDEMAND_APPS)

@app.route('/status')
def status_page():
    # (Existing route remains unchanged)
    ...

# --- API Endpoints ---

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        try:
            new_config = request.get_json()
            # Add ondemand_apps to validation
            if not all(k in new_config for k in ['tuners', 'channels', 'epg_channels', 'ondemand_apps']):
                return jsonify({"error": "Invalid configuration structure."}), 400
            with open(CONFIG_FILE_PATH, 'w') as f: json.dump(new_config, f, indent=2)
            load_config()
            os.kill(os.getppid(), signal.SIGHUP)
            return jsonify({"message": "Configuration saved. Server is reloading."}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else: # GET
        try:
            with open(CONFIG_FILE_PATH, 'r') as f: config_data = json.load(f)
            return jsonify(config_data)
        except FileNotFoundError:
            return jsonify({"tuners": [], "channels": [], "epg_channels": [], "ondemand_apps": []})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# --- NEW: Pre-Tune API ---
@app.route('/api/pretune/start', methods=['POST'])
def api_pretune_start():
    result = start_preview_session()
    status_code = 200 if result['status'] == 'success' else 503
    return jsonify(result), status_code

@app.route('/api/pretune/stop', methods=['POST'])
def api_pretune_stop():
    stop_preview_session()
    return jsonify({"status": "success", "message": "Preview session stopped."})

@app.route('/api/pretune/commit', methods=['POST'])
def api_pretune_commit():
    result = commit_preview_session()
    status_code = 200 if result['status'] == 'success' else 409
    return jsonify(result), status_code

# --- Other Remote and Status APIs ---

@app.route('/remote/launch/<device_ip>/<app_id>', methods=['POST'])
def remote_launch(device_ip, app_id):
    """New endpoint to launch an app, used by pretune page."""
    try:
        roku_session.post(f"http://{device_ip}:8060/launch/{app_id}")
        return jsonify({"status": "success"})
    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# Add other existing routes like /api/status, /remote/keypress, etc.
# ... (The rest of your app.py file remains the same)

if __name__ != '__main__':
    load_config()
