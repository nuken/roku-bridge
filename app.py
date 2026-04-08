import subprocess
import logging
from logging import StreamHandler
import json
import os
import requests
import time
import threading
import urllib.parse
import signal
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, stream_with_context, render_template
from werkzeug.utils import secure_filename

app = Flask(__name__)

# --- Application Version ---
APP_VERSION = "5.0.0-ADB"

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
# Default to android_channels.json, but fallback to roku_channels.json if that's what you have
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, 'android_channels.json')
if not os.path.exists(CONFIG_FILE_PATH) and os.path.exists(os.path.join(CONFIG_DIR, 'roku_channels.json')):
    CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, 'roku_channels.json')

DEBUG_LOGGING_ENABLED = os.getenv('ENABLE_DEBUG_LOGGING', 'false').lower() == 'true'
ENCODING_MODE = os.getenv('ENCODING_MODE', 'proxy').lower()
AUDIO_BITRATE = os.getenv('AUDIO_BITRATE', '128k')
SILENT_TS_PACKET = b'\x47\x40\x11\x10\x00\x02\xb0\x0d\x00\x01\xc1\x00\x00' + b'\xff' * 175

# --- State Management ---
TUNERS = []
CHANNELS = []
TUNER_LOCK = threading.Lock()
executor = ThreadPoolExecutor(max_workers=10)

# --- ADB Helper Functions ---
def adb_command(device_ip, cmd_list):
    """Executes an ADB command against a specific device."""
    try:
        # Connect first (idempotent)
        subprocess.run(['adb', 'connect', device_ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        
        full_cmd = ['adb', '-s', device_ip] + cmd_list
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=5)
        if DEBUG_LOGGING_ENABLED and result.stdout:
            logging.info(f"ADB {device_ip}: {result.stdout.strip()}")
        return True
    except Exception as e:
        logging.error(f"ADB Error ({device_ip}): {e}")
        return False

def check_adb_status(device_ip):
    """Checks if the device is connected to ADB."""
    try:
        # Simple ping via shell date
        cmd = ['adb', '-s', device_ip, 'shell', 'date']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return result.returncode == 0
    except:
        return False

# --- Core Application Logic ---

def load_config():
    global TUNERS, CHANNELS
    if not os.path.exists(CONFIG_FILE_PATH):
        logging.warning(f"Config file not found at {CONFIG_FILE_PATH}. Creating default.")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w') as f:
                json.dump({"tuners": [], "channels": []}, f, indent=2)
        except Exception as e:
            logging.error(f"Could not create default config: {e}")
    try:
        with open(CONFIG_FILE_PATH, 'r') as f: config_data = json.load(f) or {}
        TUNERS = sorted(config_data.get('tuners', []), key=lambda x: x.get('priority', 99))
        for tuner in TUNERS: tuner['in_use'] = False
        CHANNELS = config_data.get('channels', [])
        logging.info(f"Loaded {len(TUNERS)} tuners and {len(CHANNELS)} channels.")
    except Exception as e:
        logging.error(f"Error loading config: {e}")

def lock_tuner():
    with TUNER_LOCK:
        for tuner in TUNERS:
            if not tuner.get('in_use'):
                tuner['in_use'] = True
                logging.info(f"Locked tuner: {tuner.get('name')}")
                return tuner
    return None

def release_tuner(device_ip):
    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('device_ip') == device_ip:
                if tuner.get('in_use'):
                    tuner['in_use'] = False
                    logging.info(f"Released tuner: {tuner.get('name')}. Sending HOME.")
                    # KEYCODE_HOME = 3
                    executor.submit(adb_command, device_ip, ['shell', 'input', 'keyevent', '3'])
                    # Optional: Sleep after release? KEYCODE_SLEEP = 223
                    # executor.submit(adb_command, device_ip, ['shell', 'input', 'keyevent', '223'])
                break

def execute_tuning(device_ip, channel_data):
    video_id = channel_data.get('deep_link_content_id')
    if not video_id:
        logging.error(f"No Content ID for {channel_data['name']}")
        return

    logging.info(f"Tuning {device_ip} to YTTV ID: {video_id}")

    # 1. Wake up device (KEYCODE_WAKEUP = 224)
    adb_command(device_ip, ['shell', 'input', 'keyevent', '224'])
    time.sleep(1)

    # 2. Fire Deep Link Intent
    deep_link = f"https://tv.youtube.com/watch/{video_id}"
    adb_command(device_ip, [
        'shell', 'am', 'start',
        '-a', 'android.intent.action.VIEW',
        '-d', deep_link,
        '-n', 'com.google.android.youtube.tvunplugged/com.google.android.apps.youtube.tvunplugged.activity.MainActivity'
    ])

    # 3. Force Live (If configured as "live" media)
    # Checks the "media_type" field from your android_channels.json
    if channel_data.get('media_type') == 'live':
        def jump_to_live():
            # Wait for the app to load and stream to buffer (Adjust this 8s delay if needed)
            time.sleep(6)
            logging.info(f"Forcing Live on {device_ip}...")
            
            # COMMENTED OUT TO FIX OVERLAY ISSUE:
            # for _ in range(3):
            #    adb_command(device_ip, ['shell', 'input', 'keyevent', '90'])
            #    time.sleep(0.5)

        # Run this in the background so we don't block the stream response
        threading.Thread(target=jump_to_live).start()

def stream_generator(encoder_url, device_ip_to_release):
    try:
        # Proxy mode (standard for Link Pi)
        with requests.get(encoder_url, timeout=15, stream=True) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                yield chunk
    except Exception as e:
        logging.error(f"Stream error for {device_ip_to_release}: {e}")
    finally:
        release_tuner(device_ip_to_release)

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    locked_tuner = lock_tuner()
    if not locked_tuner: return "All tuners are in use.", 503
    
    channel_data = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel_data:
        release_tuner(locked_tuner['device_ip'])
        return "Channel not found.", 404
        
    executor.submit(execute_tuning, locked_tuner['device_ip'], channel_data)
    
    # Wait a moment for the stream to start on the Android device
    time.sleep(2)
    
    return Response(stream_with_context(stream_generator(locked_tuner['encoder_url'], locked_tuner['device_ip'])), mimetype='video/mpeg')

@app.route('/channels.m3u')
def generate_m3u():
    m3u = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    for ch in CHANNELS:
        url = f"http://{request.host}/stream/{ch['id']}"
        logo = f' tvg-logo="{ch["tvg_logo"]}"' if "tvg_logo" in ch else ""
        tvc_id = f' tvc-guide-stationid="{ch["tvc_guide_stationid"]}"' if "tvc_guide_stationid" in ch else ""
        
        m3u.append(f'#EXTINF:-1 channel-id="{ch["id"]}" tvg-name="{ch["name"]}"{logo}{tvc_id},{ch["name"]}')
        m3u.append(url)
    return Response("\n".join(m3u), mimetype='audio/x-mpegurl')

@app.route('/')
def index():
    return f"Android/ADB Bridge Running. <a href='/status'>Status</a> | <a href='/remote'>Remote</a>"

@app.route('/status')
def status_page():
    # Reuse your existing status.html template
    settings = {'app_version': APP_VERSION, 'debug_logging': DEBUG_LOGGING_ENABLED}
    return render_template('status.html', global_settings=settings)

@app.route('/api/status')
def api_status():
    def check_tuner(tuner):
        ip = tuner['device_ip']
        adb_ok = check_adb_status(ip)
        encoder_ok = False
        try:
            r = requests.get(tuner['encoder_url'], timeout=2, stream=True)
            if r.status_code == 200: encoder_ok = True
        except: pass
        
        return {
            "name": tuner.get("name"),
            "device_ip": ip,
            "roku_status": "online" if adb_ok else "offline", # Reusing key 'roku_status' for frontend compatibility
            "encoder_status": "online" if encoder_ok else "offline"
        }

    with ThreadPoolExecutor() as ex:
        statuses = list(ex.map(check_tuner, TUNERS))
    
    return jsonify({"tuners": TUNERS, "statuses": statuses})

# --- Remote Control via ADB ---
@app.route('/remote')
def remote_page():
    return render_template('remote.html')

@app.route('/remote/keypress/<device_ip>/<key>', methods=['POST'])
def remote_keypress(device_ip, key):
    # Map friendly names to ADB KeyCodes
    # https://developer.android.com/reference/android/view/KeyEvent
    key_map = {
        "Home": 3, "Back": 4, "Select": 66, "Enter": 66,
        "Up": 19, "Down": 20, "Left": 21, "Right": 22,
        "Play": 85, "Pause": 85, "Rev": 89, "Fwd": 90,
        "Info": 82, "InstantReplay": 88 # Previous media
    }
    
    adb_key = key_map.get(key)
    if not adb_key:
        # Fallback for literal characters if needed, or ignore
        logging.warning(f"Unknown key: {key}")
        return jsonify({"status": "error", "message": "Unknown key"}), 400

    adb_command(device_ip, ['shell', 'input', 'keyevent', str(adb_key)])
    return jsonify({"status": "success"})

# --- Config Management ---
@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    global TUNERS, CHANNELS
    if request.method == 'POST':
        try:
            new_config = request.get_json()
            with open(CONFIG_FILE_PATH, 'w') as f: 
                json.dump(new_config, f, indent=2)
            
            # Save the current streaming states before reloading
            old_tuners_state = {t['device_ip']: t.get('in_use', False) for t in TUNERS}
            
            load_config()
            
            # Restore the streaming states so streams don't look idle if config is saved
            for t in TUNERS:
                if t['device_ip'] in old_tuners_state:
                    t['in_use'] = old_tuners_state[t['device_ip']]
                    
            return jsonify({"message": "Saved"}), 200
        except Exception as e: 
            return jsonify({"error": str(e)}), 500
    else:
        # Return the LIVE memory state so the frontend knows if a tuner is in_use
        return jsonify({"tuners": TUNERS, "channels": CHANNELS})

if __name__ == '__main__':
    load_config()
    
    # --- NEW BLOCK: Auto-Connect on Startup ---
    logging.info("--- Starting ADB Daemon & Connecting to Tuners ---")
    for tuner in TUNERS:
        ip = tuner.get('device_ip')
        if ip:
            logging.info(f"Initializing connection to {ip}...")
            # We use a longer timeout here (10s) to allow the daemon to spin up
            try:
                subprocess.run(['adb', 'connect', ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            except Exception as e:
                logging.error(f"Startup connection failed for {ip}: {e}")
    # ------------------------------------------

    # Check for ADB presence
    try:
        subprocess.run(['adb', '--version'], stdout=subprocess.DEVNULL)
    except FileNotFoundError:
        logging.error("ERROR: 'adb' executable not found. Please install Android Platform Tools.")
        
    app.run(host='0.0.0.0', port=5000)