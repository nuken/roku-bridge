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
from werkzeug.utils import secure_filename

# --- Import Plugin System ---
from plugins import discovered_plugins

app = Flask(__name__)

# --- Application Version ---
APP_VERSION = "4.5.5"

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
TUNERS, CHANNELS, EPG_CHANNELS, ONDEMAND_APPS, ONDEMAND_SETTINGS = [], [], [], [], {}
TUNER_LOCK = threading.Lock()
KEEP_ALIVE_TASKS = {}
# --- NEW: Multi-session support for pre-tuning ---
PREVIEW_SESSIONS = {} # Keyed by tuner IP
SESSION_LOCK = threading.Lock()

roku_session = requests.Session()
roku_session.timeout = 8 # Increased timeout for better reliability
roku_session.headers.update({"Connection": "close"}) # Prevent stale connections
executor = ThreadPoolExecutor(max_workers=10) # Increased workers for more concurrent tasks

# --- Core Application Logic ---

def load_config():
    global TUNERS, CHANNELS, EPG_CHANNELS, ONDEMAND_APPS, ONDEMAND_SETTINGS
    if not os.path.exists(CONFIG_FILE_PATH):
        logging.warning(f"Config file not found at {CONFIG_FILE_PATH}. Creating default.")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w') as f:
                json.dump({"tuners": [], "channels": [], "epg_channels": [], "ondemand_apps": [], "ondemand_settings": {}}, f, indent=2)
        except Exception as e:
            logging.error(f"Could not create default config: {e}")
    try:
        with open(CONFIG_FILE_PATH, 'r') as f: config_data = json.load(f) or {}
        TUNERS = sorted(config_data.get('tuners', []), key=lambda x: x.get('priority', 99))
        for tuner in TUNERS: tuner['in_use'] = False
        CHANNELS = config_data.get('channels', [])
        EPG_CHANNELS = config_data.get('epg_channels', [])
        ONDEMAND_APPS = config_data.get('ondemand_apps', [])
        ONDEMAND_SETTINGS = config_data.get('ondemand_settings', {})
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

    was_in_preview = False
    with SESSION_LOCK:
        if tuner_ip in PREVIEW_SESSIONS:
            was_in_preview = True
            del PREVIEW_SESSIONS[tuner_ip]
            logging.info(f"Cleared preview session for tuner {tuner_ip}")

    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('roku_ip') == tuner_ip:
                if tuner.get('in_use') or was_in_preview:
                    tuner['in_use'] = False
                    logging.info(f"Released tuner: {tuner.get('name')}. Sending Home keypress.")
                    try:
                        # Send Home keypress multiple times for reliability
                        for _ in range(3):
                            roku_session.post(f"http://{tuner_ip}:8060/keypress/Home")
                            time.sleep(0.2)
                    except requests.exceptions.RequestException as e:
                        logging.error(f"Failed to send Home keypress to {tuner_ip}: {e}")
                break

def send_key_sequence(device_ip, keys):
    for i, key in enumerate(keys):
        try:
            if isinstance(key, dict) and 'wait' in key:
                time.sleep(float(key['wait']))
                continue
            if isinstance(key, str) and key.lower().startswith('wait='):
                try: duration = float(key.split('=')[1]); time.sleep(duration); continue
                except (ValueError, IndexError): logging.error(f"Invalid wait command: {key}"); continue
            
            safe_key = f"Lit_{urllib.parse.quote(key)}" if len(key) == 1 else key
            roku_session.post(f"http://{device_ip}:8060/keypress/{safe_key}")
            if DEBUG_LOGGING_ENABLED: logging.info(f"Sent key '{key}' to {device_ip}")
            
            # Use a configurable delay if provided in the channel data, otherwise default
            custom_delay = next((float(k.split('=')[1]) for k in keys[i+1:] if isinstance(k, str) and k.startswith('delay=')), 0.5)
            time.sleep(custom_delay)

        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to send key '{key}' to {device_ip}: {e}")
            # --- NEW: Retry mechanism ---
            for attempt in range(2): # Retry up to 2 times
                time.sleep(1) # Wait before retrying
                try:
                    roku_session.post(f"http://{device_ip}:8060/keypress/{safe_key}")
                    logging.info(f"Successfully sent key '{key}' on retry {attempt + 1}")
                    break
                except requests.exceptions.RequestException:
                    if attempt == 1:
                        logging.error(f"Failed to send key '{key}' after multiple retries.")
                        return False # Abort sequence on persistent failure
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

# --- Pre-Tune Session Management ---
def start_preview_session(tuner_ip):
    with TUNER_LOCK:
        tuner = next((t for t in TUNERS if t['roku_ip'] == tuner_ip), None)
        if not tuner:
            return {"status": "error", "message": "Tuner not found."}
        if tuner.get('in_use'):
            return {"status": "error", "message": "Tuner is already in use."}
        tuner['in_use'] = True

    with SESSION_LOCK:
        PREVIEW_SESSIONS[tuner_ip] = {'tuner': tuner, 'committed': False}
        logging.info(f"Started preview session on tuner {tuner['name']}")
        return {"status": "success", "tuner_name": tuner['name'], "roku_ip": tuner['roku_ip']}

def stop_preview_session(tuner_ip):
    # This function is now just a wrapper for release_tuner for clarity
    release_tuner(tuner_ip)
    return {"status": "success", "message": "Session stopped."}

def commit_preview_session(tuner_ip):
    with SESSION_LOCK:
        if tuner_ip not in PREVIEW_SESSIONS:
            return {"status": "error", "message": "No active preview session to commit."}
        PREVIEW_SESSIONS[tuner_ip]['committed'] = True
        tuner_name = PREVIEW_SESSIONS[tuner_ip]['tuner']['name']
        logging.info(f"Committed preview session for tuner {tuner_name}.")
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
    tuner_ip = request.args.get('tuner_ip')
    if not tuner_ip:
        return "Tuner IP is required.", 400

    with SESSION_LOCK:
        session = PREVIEW_SESSIONS.get(tuner_ip)
        if not session or not session['committed']:
            return "No pre-tuned stream is ready for this tuner.", 404
        tuner = session['tuner']

    logging.info(f"Channels DVR connected to committed stream from tuner {tuner['name']}")
    time.sleep(2) # Give a moment for connection

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
        
        # --- START OF FIX ---
        # Expanded the tags dictionary to include all possible custom EPG fields.
        tags = {
            "tvg-name": "name",
            "channel-number": "channel-number",
            "tvg-logo": "tvg-logo",
            "tvc-guide-stationid": "tvc_guide_stationid",
            "tvc-guide-art": "tvc-guide-art",
            "tvc-guide-title": "tvc-guide-title",
            "tvc-guide-description": "tvc-guide-description",
            "tvc-guide-tags": "tvc-guide-tags",
            "tvc-guide-genres": "tvc-guide-genres",
            "tvc-guide-categories": "tvc-guide-categories",
            "tvc-guide-placeholders": "tvc-guide-placeholders",
            "tvc-stream-vcodec": "tvc-stream-vcodec",
            "tvc-stream-acodec": "tvc-stream-acodec"
        }
        # --- END OF FIX ---

        for tag, key in tags.items():
            if key in channel and channel[key]:
                # For tags that can be comma-separated lists, ensure they are formatted correctly.
                if isinstance(channel[key], list):
                    extinf_line += f' {tag}="{",".join(map(str, channel[key]))}"'
                else:
                    extinf_line += f' {tag}="{channel[key]}"'

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
    for tuner in TUNERS:
        tuner_name = tuner.get("name", tuner['roku_ip'])
        channel_id = f"ondemand_stream_{tuner_name.replace(' ', '_')}"
        stream_url = f"http://{request.host}/stream/ondemand_stream?tuner_ip={tuner['roku_ip']}"
        channel_name = f"On-Demand Stream ({tuner_name})"
        extinf_line = f'#EXTINF:-1 channel-id="{channel_id}" tvg-name="{channel_name}"'
        if ONDEMAND_SETTINGS.get('tvg_logo'):
            extinf_line += f' tvg-logo="{ONDEMAND_SETTINGS["tvg_logo"]}"'
        if ONDEMAND_SETTINGS.get('tvc_guide_art'):
            extinf_line += f' tvc-guide-art="{ONDEMAND_SETTINGS["tvc_guide_art"]}"'

        # --- THIS IS THE FIX ---
        extinf_line += f',{channel_name}'
        # --- END OF FIX ---

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

@app.route('/logs')
def logs_page():
    if not DEBUG_LOGGING_ENABLED: return "Debug logging is not enabled.", 404
    return render_template('logs.html')

@app.route('/logs/content')
def logs_content():
    if not DEBUG_LOGGING_ENABLED: return "Debug logging is not enabled.", 404
    return Response("\n".join(log_buffer), mimetype='text/plain')

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

# --- UPDATED API ENDPOINT ---
@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        try:
            new_config = request.get_json()
            
            # --- START OF FIX: Sanitize Roku IP addresses ---
            if 'tuners' in new_config and isinstance(new_config['tuners'], list):
                for tuner in new_config['tuners']:
                    if 'roku_ip' in tuner and isinstance(tuner['roku_ip'], str):
                        ip = tuner['roku_ip'].lower().strip()
                        if ip.startswith('http://'):
                            ip = ip[7:]
                        elif ip.startswith('https://'):
                            ip = ip[8:]
                        tuner['roku_ip'] = ip
            # --- END OF FIX ---

            validated_config = {
                "tuners": new_config.get("tuners", []),
                "channels": new_config.get("channels", []),
                "epg_channels": new_config.get("epg_channels", []),
                "ondemand_apps": new_config.get("ondemand_apps", []),
                "ondemand_settings": new_config.get("ondemand_settings", {})
            }
            with open(CONFIG_FILE_PATH, 'w') as f: json.dump(validated_config, f, indent=2)
            load_config()
            os.kill(os.getppid(), signal.SIGHUP)
            return jsonify({"message": "Configuration saved. Server is reloading."}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else: # GET
        try:
            with open(CONFIG_FILE_PATH, 'r') as f: config_data = json.load(f)
            config_data['ondemand_apps'] = config_data.get('ondemand_apps', [])
            config_data['ondemand_settings'] = config_data.get('ondemand_settings', {})
            return jsonify(config_data)
        except FileNotFoundError:
            return jsonify({"tuners": [], "channels": [], "epg_channels": [], "ondemand_apps": [], "ondemand_settings": {}})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route('/upload_config', methods=['POST'])
def upload_config():
    if 'file' not in request.files: return "No file part", 400
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.json'): return "Invalid file", 400
    try:
        filename = secure_filename(file.filename)
        file.save(CONFIG_FILE_PATH)
        load_config()
        os.kill(os.getppid(), signal.SIGHUP)
        return "Configuration updated successfully. Server is reloading...", 200
    except Exception as e:
        return f"Error processing config file: {e}", 400

@app.route('/upload_plugin', methods=['POST'])
def upload_plugin():
    if 'file' not in request.files: return "No file part", 400
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('_plugin.py'): return "Invalid file. Must be a '_plugin.py' file.", 400
    try:
        plugins_dir = os.path.join(os.path.dirname(__file__), 'plugins')
        os.makedirs(plugins_dir, exist_ok=True)
        filename = secure_filename(file.filename)
        save_path = os.path.join(plugins_dir, filename)
        if not os.path.normpath(save_path).startswith(os.path.abspath(plugins_dir)):
            return "Invalid filename", 400
        file.save(save_path)
        logging.info(f"New plugin uploaded: {filename}")
        os.kill(os.getppid(), signal.SIGHUP)
        return "Plugin uploaded successfully. Server is reloading...", 200
    except Exception as e:
        logging.error(f"Error saving plugin: {e}")
        return f"Error saving plugin file: {e}", 500

# --- NEW Pre-Tune API ---
@app.route('/api/preview/stop', methods=['POST'])
def api_preview_stop():
    with TUNER_LOCK:
        for tuner in TUNERS:
            with SESSION_LOCK:
                is_in_preview_session = tuner['roku_ip'] in PREVIEW_SESSIONS
            if tuner['in_use'] and not is_in_preview_session:
                release_tuner(tuner['roku_ip'])
                return jsonify({"status": "success", "message": f"Released tuner {tuner.get('name')}"})
    return jsonify({"status": "error", "message": "No active preview stream tuner found to release."})

@app.route('/api/pretune/status')
def api_pretune_status():
    with SESSION_LOCK:
        active_ips = set(PREVIEW_SESSIONS.keys())
    status = []
    for tuner in TUNERS:
        tuner_status = "in-use" if tuner['in_use'] else "available"
        if tuner['roku_ip'] in active_ips:
            tuner_status = "pre-tuning"
        status.append({
            "name": tuner.get("name", tuner['roku_ip']),
            "roku_ip": tuner['roku_ip'],
            "status": tuner_status
        })
    return jsonify(status)

@app.route('/api/pretune/start', methods=['POST'])
def api_pretune_start():
    tuner_ip = request.json.get('tuner_ip')
    if not tuner_ip: return jsonify({"status": "error", "message": "Tuner IP is required."}), 400
    result = start_preview_session(tuner_ip)
    status_code = 200 if result['status'] == 'success' else 503
    return jsonify(result), status_code

@app.route('/api/pretune/stop', methods=['POST'])
def api_pretune_stop():
    tuner_ip = request.json.get('tuner_ip')
    if not tuner_ip: return jsonify({"status": "error", "message": "Tuner IP is required."}), 400
    result = stop_preview_session(tuner_ip)
    return jsonify(result)

@app.route('/api/pretune/commit', methods=['POST'])
def api_pretune_commit():
    tuner_ip = request.json.get('tuner_ip')
    if not tuner_ip: return jsonify({"status": "error", "message": "Tuner IP is required."}), 400
    result = commit_preview_session(tuner_ip)
    status_code = 200 if result['status'] == 'success' else 409
    return jsonify(result), status_code

@app.route('/api/pretune/stream')
def api_pretune_stream():
    tuner_ip = request.args.get('tuner_ip')
    with SESSION_LOCK:
        if tuner_ip not in PREVIEW_SESSIONS:
            return "No active preview session for this tuner.", 404
        tuner = PREVIEW_SESSIONS[tuner_ip]['tuner']
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
    with SESSION_LOCK:
        is_in_preview = device_ip in PREVIEW_SESSIONS
    if not any(t['roku_ip'] == device_ip for t in TUNERS) and not is_in_preview:
        return jsonify({"status": "error", "message": "Device not found or not in a session."}), 404
    try:
        roku_session.post(f"http://{device_ip}:8060/keypress/{urllib.parse.quote(key)}")
        return jsonify({"status": "success"})
    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/remote/reboot/<device_ip>', methods=['POST'])
def remote_reboot(device_ip):
    if not any(t['roku_ip'] == device_ip for t in TUNERS): return jsonify({"status": "error", "message": "Device not found."}), 404
    reboot_sequence = ['Home', 'Home', 'Home', 'Up', 'Right', 'Up', 'Right', 'Up', 'Up', 'Right', 'Select']
    executor.submit(send_key_sequence, device_ip, reboot_sequence)
    return jsonify({"status": "success", "message": "Reboot sequence initiated."})

@app.route('/remote/devices')
def get_remote_devices():
    return jsonify([{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"]} for t in TUNERS])

@app.route('/api/status')
def api_status():
    def check_tuner_status(tuner):
        roku_ip = tuner['roku_ip']
        encoder_url = tuner['encoder_url']
        roku_status, roku_error = 'offline', 'Unknown Error'
        encoder_status, encoder_error = 'offline', 'Unknown Error'

        try:
            # Increased timeout and added specific error handling for Roku
            roku_session.get(f"http://{roku_ip}:8060", timeout=8)
            roku_status = 'online'
            roku_error = ''
        except requests.exceptions.Timeout:
            roku_error = 'Timeout'
        except requests.exceptions.ConnectionError:
            roku_error = 'Connection Refused'
        except requests.exceptions.RequestException as e:
            roku_error = str(e)

        try:
            # Increased timeout and added specific error handling for Encoder
            with requests.get(encoder_url, timeout=10, stream=True, allow_redirects=True) as response:
                response.raise_for_status()
                if next(response.iter_content(1), None):
                    encoder_status = 'online'
                    encoder_error = ''
        except requests.exceptions.Timeout:
            encoder_error = 'Timeout'
        except requests.exceptions.ConnectionError:
            encoder_error = 'Connection Refused'
        except requests.exceptions.RequestException as e:
            encoder_error = f'HTTP {response.status_code}' if 'response' in locals() else str(e)

        return {
            "name": tuner.get("name", roku_ip),
            "roku_ip": roku_ip,
            "encoder_url": encoder_url,
            "roku_status": roku_status,
            "roku_error": roku_error,
            "encoder_status": encoder_status,
            "encoder_error": encoder_error
        }

    with ThreadPoolExecutor(max_workers=len(TUNERS) or 1) as status_executor:
        statuses = list(status_executor.map(check_tuner_status, TUNERS))

    tuner_configs = [{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"], "encoder_url": t["encoder_url"]} for t in TUNERS]
    return jsonify({"tuners": tuner_configs, "statuses": statuses})


@app.route('/api/plugins')
def api_plugins():
    plugin_list = [{"id": script_name, "name": plugin.app_name} for script_name, plugin in discovered_plugins.items()]
    return jsonify(plugin_list)

if __name__ != '__main__':

    load_config()