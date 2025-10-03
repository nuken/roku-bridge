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
APP_VERSION = "4.5-stream" # Updated Version

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
PREVIEW_SESSION = {'tuner': None, 'active': False, 'committed': False}
SESSION_LOCK = threading.Lock()


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
        ONDEMAND_APPS = config_data.get('ondemand_apps', [])
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
    with SESSION_LOCK:
        if PREVIEW_SESSION['tuner'] and PREVIEW_SESSION['tuner']['roku_ip'] == tuner_ip:
            PREVIEW_SESSION.update({'tuner': None, 'active': False, 'committed': False})
            logging.info(f"Cleared preview session associated with tuner {tuner_ip}")
    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('roku_ip') == tuner_ip:
                if tuner['in_use']:
                    tuner['in_use'] = False
                    if DEBUG_LOGGING_ENABLED: logging.info(f"Released tuner: {tuner.get('name')}")
                    try:
                        roku_session.post(f"http://{tuner_ip}:8060/keypress/Home")
                    except requests.exceptions.RequestException:
                        pass
                break

def send_key_sequence(device_ip, keys):
    for key in keys:
        try:
            if isinstance(key, dict) and 'wait' in key:
                time.sleep(float(key['wait']))
                continue
            if isinstance(key, str) and key.lower().startswith('wait='):
                try:
                    duration = float(key.split('=')[1])
                    time.sleep(duration)
                    continue
                except (ValueError, IndexError):
                    logging.error(f"Invalid wait command: {key}")
                    continue
            safe_key = f"Lit_{urllib.parse.quote(key)}" if len(key) == 1 else key
            roku_session.post(f"http://{device_ip}:8060/keypress/{safe_key}")
            if DEBUG_LOGGING_ENABLED: logging.info(f"Sent key '{key}' to {device_ip}")
            time.sleep(0.5)
        except Exception as e:
            logging.error(f"Failed to send key '{key}' to {device_ip}: {e}")
            return False
    return True

def keep_alive_sender(roku_ip, key_string, interval_minutes, stop_event):
    keys = [k.strip() for k in key_string.split(',')]
    interval_seconds = interval_minutes * 60
    while not stop_event.wait(interval_seconds):
        try:
            logging.info(f"[Keep-Alive] Sending sequence {keys} to {roku_ip} to prevent timeout.")
            send_key_sequence(roku_ip, keys)
        except Exception as e:
            logging.error(f"[Keep-Alive] Error sending key sequence to {roku_ip}: {e}")

def execute_tuning_in_background(roku_ip, channel_data):
    try:
        if DEBUG_LOGGING_ENABLED: logging.info(f"Tuning to actual channel {channel_data['name']}...")
        launch_url = f"http://{roku_ip}:8060/launch/{channel_data['roku_app_id']}"
        roku_session.post(launch_url)
        time.sleep(channel_data.get("tune_delay", 1))
        plugin_script = channel_data.get('plugin_script')
        key_sequence = channel_data.get('key_sequence')
        if plugin_script and plugin_script in discovered_plugins:
            plugin = discovered_plugins[plugin_script]
            final_sequence = plugin.tune_channel(roku_ip, channel_data)
            if final_sequence: send_key_sequence(roku_ip, final_sequence)
        elif key_sequence:
            send_key_sequence(roku_ip, key_sequence)
        else:
            content_id = channel_data.get('deep_link_content_id')
            if content_id:
                media_type = channel_data.get('media_type', 'live')
                params = f"?contentId={content_id}&mediaType={media_type}"
                roku_session.post(f"{launch_url}{params}")
        if channel_data.get('needs_select_keypress'):
            time.sleep(1)
            send_key_sequence(roku_ip, ["Select"])
    except Exception as e:
        logging.error(f"Error during background tuning for {roku_ip}: {e}")

def stream_generator(encoder_url, roku_ip_to_release, mode='proxy', blank_duration=0):
    try:
        if blank_duration > 0:
            start_time = time.time()
            while time.time() - start_time < blank_duration:
                yield SILENT_TS_PACKET
                time.sleep(0.1)
        if mode in ['remux', 'reencode']:
            command = ['ffmpeg', '-i', encoder_url]
            if mode == 'reencode':
                command.extend(['-c:v', 'copy', '-c:a', 'aac', '-b:a', AUDIO_BITRATE, '-ac', AUDIO_CHANNELS])
            else:
                command.extend(['-c', 'copy'])
            command.extend(['-f', 'mpegts', '-loglevel', 'error', '-'])
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for chunk in iter(lambda: process.stdout.read(8192), b''): yield chunk
            process.wait()
        else: # Proxy
            with requests.get(encoder_url, timeout=15, stream=True, allow_redirects=True) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=8192):
                    yield chunk
    except Exception as e:
        logging.error(f"Stream error for {roku_ip_to_release} ({mode}): {e}")
    finally:
        release_tuner(roku_ip_to_release)

def start_preview_session():
    with SESSION_LOCK:
        if PREVIEW_SESSION['active']:
            return {"status": "error", "message": "A preview session is already active."}
        tuner = lock_tuner()
        if not tuner:
            return {"status": "error", "message": "All tuners are in use."}
        PREVIEW_SESSION.update({'tuner': tuner, 'active': True, 'committed': False})
        logging.info(f"Started preview session on tuner {tuner['name']}")
        return {"status": "success", "tuner_name": tuner['name'], "roku_ip": tuner['roku_ip']}

def stop_preview_session():
    with SESSION_LOCK:
        tuner = PREVIEW_SESSION.get('tuner')
    if tuner:
        release_tuner(tuner['roku_ip'])

def commit_preview_session():
    with SESSION_LOCK:
        if not PREVIEW_SESSION['active'] or not PREVIEW_SESSION['tuner']:
            return {"status": "error", "message": "No active preview session to commit."}
        PREVIEW_SESSION['committed'] = True
        logging.info(f"Committed preview session for tuner {PREVIEW_SESSION['tuner']['name']}. It is now ready for Channels DVR.")
        return {"status": "success", "message": "Stream is now ready for Channels DVR."}

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    is_preview = request.args.get('preview', 'false').lower() == 'true'
    locked_tuner = lock_tuner()
    if not locked_tuner: return "All tuners are in use.", 503
    channel_data = next((c for c in CHANNELS + EPG_CHANNELS if c["id"] == channel_id), None)
    if not channel_data:
        release_tuner(locked_tuner['roku_ip'])
        return "Channel not found.", 404
    executor.submit(execute_tuning_in_background, locked_tuner['roku_ip'], channel_data)
    if channel_data.get('keep_alive_enabled') and channel_data.get('keep_alive_key'):
        interval = channel_data.get('keep_alive_interval', 225)
        stop_event = threading.Event()
        thread = threading.Thread(target=keep_alive_sender, args=(locked_tuner['roku_ip'], channel_data['keep_alive_key'], interval, stop_event))
        thread.daemon = True
        thread.start()
        KEEP_ALIVE_TASKS[locked_tuner['roku_ip']] = (thread, stop_event)
    tuner_mode = locked_tuner.get('encoding_mode', ENCODING_MODE)
    blank_duration = 0 if is_preview else channel_data.get('blank_duration', 0)
    generator = stream_generator(locked_tuner['encoder_url'], locked_tuner['roku_ip'], tuner_mode, blank_duration)
    return Response(stream_with_context(generator), mimetype='video/mpeg')

@app.route('/stream/ondemand_stream')
def stream_ondemand():
    with SESSION_LOCK:
        if not PREVIEW_SESSION['committed'] or not PREVIEW_SESSION['tuner']:
            return "No pre-tuned stream is ready. Please select content from the Pre-Tune page.", 404
        tuner = PREVIEW_SESSION['tuner']
    
    logging.info(f"Channels DVR connected to committed stream from tuner {tuner['name']}")
    time.sleep(2)
    
    tuner_mode = tuner.get('encoding_mode', ENCODING_MODE)
    generator = stream_generator(tuner['encoder_url'], tuner['roku_ip'], tuner_mode)
    return Response(stream_with_context(generator), mimetype='video/mpeg')

def generate_m3u_from_channels(channel_list, playlist_filter=None):
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    filtered_list = channel_list
    if playlist_filter:
        filtered_list = [ch for ch in channel_list if ch.get('playlist') == playlist_filter]
        logging.info(f"Filtering M3U for playlist='{playlist_filter}'. Found {len(filtered_list)} matching channels.")
    for channel in filtered_list:
        stream_url = f"http://{request.host}/stream/{channel['id']}"
        extinf_line = f'#EXTINF:-1 channel-id="{channel["id"]}"'
        tags = { "tvg-name": "name", "channel-number": "channel-number", "tvg-logo": "tvg-logo", "tvc-guide-stationid": "tvc_guide_stationid" }
        for tag, key in tags.items():
            if key in channel: extinf_line += f' {tag}="{channel[key]}"'
        if 'playlist' in channel and channel['playlist']:
            extinf_line += f' group-title="{channel["playlist"]}"'
        extinf_line += f',{channel["name"]}'
        m3u_content.extend([extinf_line, stream_url])
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

@app.route('/channels.m3u')
def generate_gracenote_m3u():
    playlist_filter = request.args.get('playlist')
    return generate_m3u_from_channels(CHANNELS, playlist_filter)

@app.route('/epg_channels.m3u')
def generate_epg_m3u():
    playlist_filter = request.args.get('playlist')
    return generate_m3u_from_channels(EPG_CHANNELS, playlist_filter)

@app.route('/ondemand.m3u')
def generate_ondemand_m3u():
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    stream_url = f"http://{request.host}/stream/ondemand_stream"
    extinf_line = f'#EXTINF:-1 channel-id="ondemand_viewer" tvg-name="On-Demand Stream",On-Demand Stream'
    m3u_content.extend([extinf_line, stream_url])
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

@app.route('/')
def index():
    return f"Roku Channels Bridge is running. <a href='/status'>View Status</a> | <a href='/remote'>Go to Remote</a> | <a href='/preview'>Live TV Preview</a> | <a href='/pretune'>On-Demand Pre-Tune</a>"

@app.route('/remote')
def remote_control():
    return render_template('remote.html')

@app.route('/preview')
def preview():
    all_channels = sorted(CHANNELS + EPG_CHANNELS, key=lambda x: x.get('name', '').lower())
    return render_template('preview.html', channels=all_channels)

@app.route('/pretune')
def pretune_page():
    return render_template('pretune.html', ondemand_apps=ONDEMAND_APPS)

# --- THIS IS THE FIX for the 404 error ---
@app.route('/logs')
def logs_page():
    """Renders the log viewer page."""
    # This check ensures the page is only accessible if debug logging is enabled
    if not DEBUG_LOGGING_ENABLED:
        return "Debug logging is not enabled.", 404
    return render_template('logs.html')

@app.route('/logs/content')
def logs_content():
    """Returns the buffered logs as plain text."""
    if not DEBUG_LOGGING_ENABLED:
        return "Debug logging is not enabled.", 404
    return Response("\n".join(log_buffer), mimetype='text/plain')
# --- END OF FIX ---

@app.route('/status')
def status_page():
    settings = {
        'encoding_mode': ENCODING_MODE,
        'audio_bitrate': AUDIO_BITRATE,
        'audio_channels': os.getenv('AUDIO_CHANNELS', '2'),
        'debug_logging': DEBUG_LOGGING_ENABLED,
        'app_version': APP_VERSION
    }
    return render_template('status.html', global_settings=settings)

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        try:
            new_config = request.get_json()
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

@app.route('/api/pretune/stream')
def api_pretune_stream():
    with SESSION_LOCK:
        if not PREVIEW_SESSION['active'] or not PREVIEW_SESSION['tuner']:
            return "No active preview session.", 404
        tuner = PREVIEW_SESSION['tuner']
        encoder_url = tuner['encoder_url']
    try:
        req = requests.get(encoder_url, stream=True, timeout=10)
        return Response(stream_with_context(req.iter_content(chunk_size=8192)), content_type=req.headers['content-type'])
    except Exception as e:
        logging.error(f"Error proxying pretune stream from {encoder_url}: {e}")
        return "Failed to connect to encoder.", 500

@app.route('/remote/launch/<device_ip>/<app_id>', methods=['POST'])
def remote_launch(device_ip, app_id):
    try:
        roku_session.post(f"http://{device_ip}:8060/launch/{app_id}")
        return jsonify({"status": "success"})
    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/remote/keypress/<device_ip>/<key>', methods=['POST'])
def remote_keypress(device_ip, key):
    if not any(t['roku_ip'] == device_ip for t in TUNERS) and (not PREVIEW_SESSION['tuner'] or PREVIEW_SESSION['tuner']['roku_ip'] != device_ip):
        return jsonify({"status": "error", "message": "Device not found or not locked for preview."}), 404
    try:
        roku_session.post(f"http://{device_ip}:8060/keypress/{urllib.parse.quote(key)}")
        return jsonify({"status": "success"})
    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/remote/devices')
def get_remote_devices():
    return jsonify([{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"]} for t in TUNERS])
    
@app.route('/api/status')
def api_status():
    statuses = []
    def check_tuner_status(tuner):
        roku_ip = tuner['roku_ip']
        encoder_url = tuner['encoder_url']
        roku_status = 'offline'
        encoder_status = 'offline'
        try:
            roku_session.get(f"http://{roku_ip}:8060", timeout=3)
            roku_status = 'online'
        except requests.exceptions.RequestException: pass
        try:
            with requests.get(encoder_url, timeout=5, stream=True, allow_redirects=True) as response:
                response.raise_for_status()
                if next(response.iter_content(1), None):
                    encoder_status = 'online'
        except requests.exceptions.RequestException: pass
        return { "name": tuner.get("name", roku_ip), "roku_ip": roku_ip, "encoder_url": encoder_url, "roku_status": roku_status, "encoder_status": encoder_status }
    with ThreadPoolExecutor(max_workers=len(TUNERS) or 1) as status_executor:
        statuses = list(status_executor.map(check_tuner_status, TUNERS))
    tuner_configs = [{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"], "encoder_url": t["encoder_url"]} for t in TUNERS]
    return jsonify({"tuners": tuner_configs, "statuses": statuses})

if __name__ != '__main__':
    load_config()