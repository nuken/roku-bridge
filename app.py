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
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, stream_with_context, render_template
from werkzeug.utils import secure_filename

# --- Import Plugin System ---
from plugins import discovered_plugins

app = Flask(__name__)

# --- Application Version ---
APP_VERSION = "4.9.8-fix"

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
TMDB_API_KEY = ''
CHANNELS_DVR_IP = ''

def get_audio_channels():
    channels_input = os.getenv('AUDIO_CHANNELS', '2').lower()
    return '6' if channels_input == "5.1" else '8' if channels_input == "7.1" else channels_input
AUDIO_CHANNELS = get_audio_channels()

# --- State Management ---
TUNERS, CHANNELS, EPG_CHANNELS, ONDEMAND_APPS, ONDEMAND_SETTINGS = [], [], [], [], {}
TUNER_LOCK = threading.Lock()
KEEP_ALIVE_TASKS = {}
PREVIEW_SESSIONS = {} 
SESSION_LOCK = threading.Lock()
RECORDING_TASKS = {}

roku_session = requests.Session()
roku_session.timeout = 3
executor = ThreadPoolExecutor(max_workers=10)

# --- Core Application Logic ---

def load_config():
    global TUNERS, CHANNELS, EPG_CHANNELS, ONDEMAND_APPS, ONDEMAND_SETTINGS, TMDB_API_KEY, CHANNELS_DVR_IP
    if not os.path.exists(CONFIG_FILE_PATH):
        logging.warning(f"Config file not found at {CONFIG_FILE_PATH}. Creating default.")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w') as f:
                json.dump({"tuners": [], "channels": [], "epg_channels": [], "ondemand_apps": [], "ondemand_settings": {}, "tmdb_api_key": "", "channels_dvr_ip": ""}, f, indent=2)
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
        TMDB_API_KEY = config_data.get('tmdb_api_key', '')
        CHANNELS_DVR_IP = config_data.get('channels_dvr_ip', '')
        if DEBUG_LOGGING_ENABLED:
            logging.info(f"Loaded {len(TUNERS)} tuners, {len(CHANNELS)} Gracenote, {len(EPG_CHANNELS)} EPG channels, {len(ONDEMAND_APPS)} On-Demand apps.")
        if TMDB_API_KEY: logging.info("TMDb API Key is configured.")
        if CHANNELS_DVR_IP: logging.info(f"Channels DVR IP is configured: {CHANNELS_DVR_IP}")

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
        if tuner_ip in PREVIEW_SESSIONS:
            del PREVIEW_SESSIONS[tuner_ip]
            logging.info(f"Cleared preview session for tuner {tuner_ip}")

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

def handle_ondemand_recording(tuner_ip, duration_minutes, metadata, dvr_info_event):
    try:
        logging.info(f"Recording task started for {tuner_ip}, duration: {duration_minutes} mins.")
        
        dvr_info_event.wait(timeout=30)
        
        if not CHANNELS_DVR_IP:
            logging.error("[Recording] Channels DVR IP is not configured. Cannot send record command.")
            return

        tuner_name = next((t.get("name", t['roku_ip']) for t in TUNERS if t['roku_ip'] == tuner_ip), "Unknown")
        
        # Get the list of channels from the DVR
        try:
            dvr_channels_res = requests.get(f"http://{CHANNELS_DVR_IP}:8089/devices/ANY/channels", timeout=10)
            dvr_channels_res.raise_for_status()
            dvr_channels = dvr_channels_res.json()
        except Exception as e:
            logging.error(f"[Recording] Failed to get channels from DVR at {CHANNELS_DVR_IP}: {e}")
            return
            
        # Find the correct channel ID
        ondemand_channel_id = None
        for channel in dvr_channels:
            if channel.get('GuideName') == f"On-Demand Stream ({tuner_name})":
                ondemand_channel_id = channel.get('ID')
                break
        
        if not ondemand_channel_id:
            logging.error(f"[Recording] Could not find on-demand channel for tuner {tuner_name} in Channels DVR.")
            return

        try:
            current_time = int(time.time())
            duration_seconds = duration_minutes * 60
            
            airing_details = {
                "Source": "manual",
                "Channel": ondemand_channel_id,
                "Time": current_time,
                "Duration": duration_seconds,
                "Title": metadata.get('title') or "On-Demand Recording",
                "EpisodeTitle": metadata.get('subtitle'),
                "Summary": metadata.get('description'),
                "Image": metadata.get('image'),
                "Genres": ["On-Demand"]
            }
            # Remove any keys with None values
            airing_details = {k: v for k, v in airing_details.items() if v is not None}

            recording_payload = {
                "Name": metadata.get('title') or "On-Demand Recording",
                "Time": current_time,
                "Duration": duration_seconds,
                "Channels": [ondemand_channel_id],
                "Airing": airing_details
            }

            record_res = requests.post(f"http://{CHANNELS_DVR_IP}:8089/dvr/jobs/new", json=recording_payload, timeout=10)
            record_res.raise_for_status()
            logging.info(f"[Recording] Successfully sent record command to DVR for tuner {tuner_ip}.")
            
        except Exception as e:
            logging.error(f"[Recording] Failed to send record command to DVR at {CHANNELS_DVR_IP}: {e}")
            
    except Exception as e:
        logging.error(f"An unexpected error occurred in the recording thread: {e}")

def start_preview_session(tuner_ip):
    with TUNER_LOCK:
        tuner = next((t for t in TUNERS if t['roku_ip'] == tuner_ip), None)
        if not tuner: return {"status": "error", "message": "Tuner not found."}
        if tuner.get('in_use'): return {"status": "error", "message": "Tuner is already in use."}
        tuner['in_use'] = True
    
    with SESSION_LOCK:
        PREVIEW_SESSIONS[tuner_ip] = {
            'tuner': tuner, 
            'committed': False, 
            'dvr_info_event': threading.Event()
        }
        logging.info(f"Started preview session on tuner {tuner['name']}")
        return {"status": "success", "tuner_name": tuner['name'], "roku_ip": tuner['roku_ip']}

def stop_preview_session(tuner_ip):
    release_tuner(tuner_ip)
    return {"status": "success", "message": "Session stopped."}

def commit_preview_session(tuner_ip, record=False, duration=0, metadata=None):
    with SESSION_LOCK:
        if tuner_ip not in PREVIEW_SESSIONS: return {"status": "error", "message": "No active preview session."}
        
        session = PREVIEW_SESSIONS[tuner_ip]
        session['committed'] = True
        tuner_name = session['tuner']['name']
        
        if record and duration > 0:
            logging.info(f"Committing preview session for tuner {tuner_name} WITH recording for {duration} minutes.")
            recording_thread = threading.Thread(target=handle_ondemand_recording, args=(tuner_ip, duration, metadata, session['dvr_info_event']))
            recording_thread.daemon = True
            recording_thread.start()
            RECORDING_TASKS[tuner_ip] = recording_thread
            return {"status": "success", "message": "Stream is ready. Recording will start when you tune in."}
        else:
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
        thread.daemon = True; thread.start()
        KEEP_ALIVE_TASKS[locked_tuner['roku_ip']] = (thread, stop_event)
    tuner_mode = locked_tuner.get('encoding_mode', ENCODING_MODE)
    blank_duration = 0 if is_preview else channel_data.get('blank_duration', 0)
    generator = stream_generator(locked_tuner['encoder_url'], locked_tuner['roku_ip'], tuner_mode, blank_duration)
    return Response(stream_with_context(generator), mimetype='video/mpeg')

@app.route('/stream/ondemand_stream')
def stream_ondemand():
    tuner_ip = request.args.get('tuner_ip')
    if not tuner_ip: return "Tuner IP is required.", 400
    
    with SESSION_LOCK:
        session = PREVIEW_SESSIONS.get(tuner_ip)
        if not session or not session['committed']: return "No pre-tuned stream is ready.", 404
        tuner = session['tuner']
        session['dvr_info_event'].set()

    logging.info(f"Channels DVR ({request.remote_addr}) connected to committed stream from tuner {tuner['name']}")
    
    tuner_mode = tuner.get('encoding_mode', ENCODING_MODE)
    generator = stream_generator(tuner['encoder_url'], tuner['roku_ip'], tuner_mode)
    return Response(stream_with_context(generator), mimetype='video/mpeg')

def generate_m3u_from_channels(channel_list, playlist_filter=None):
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    filtered_list = [ch for ch in channel_list if not playlist_filter or ch.get('playlist') == playlist_filter]
    for channel in filtered_list:
        stream_url = f"http://{request.host}/stream/{channel['id']}"
        extinf_line = f'#EXTINF:-1 channel-id="{channel["id"]}"'
        tags = {"tvg-name": "name", "channel-number": "channel-number", "tvg-logo": "tvg-logo", "tvc-guide-stationid": "tvc_guide_stationid"}
        for tag, key in tags.items():
            if key in channel: extinf_line += f' {tag}="{channel[key]}"'
        if 'playlist' in channel and channel['playlist']: extinf_line += f' group-title="{channel["playlist"]}"'
        extinf_line += f',{channel["name"]}'
        m3u_content.extend([extinf_line, stream_url])
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

@app.route('/channels.m3u')
def generate_gracenote_m3u(): return generate_m3u_from_channels(CHANNELS, request.args.get('playlist'))

@app.route('/epg_channels.m3u')
def generate_epg_m3u(): return generate_m3u_from_channels(EPG_CHANNELS, request.args.get('playlist'))

@app.route('/ondemand.m3u')
def generate_ondemand_m3u():
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    for tuner in TUNERS:
        tuner_name = tuner.get("name", tuner['roku_ip'])
        channel_id = f"ondemand_stream_{tuner_name.replace(' ', '_')}"
        stream_url = f"http://{request.host}/stream/ondemand_stream?tuner_ip={tuner['roku_ip']}"
        channel_name = f"On-Demand Stream ({tuner_name})"
        extinf_line = f'#EXTINF:-1 channel-id="{channel_id}" tvg-name="{channel_name}"'
        if ONDEMAND_SETTINGS.get('tvg_logo'): extinf_line += f' tvg-logo="{ONDEMAND_SETTINGS["tvg_logo"]}"'
        if ONDEMAND_SETTINGS.get('tvc_guide_art'): extinf_line += f' tvc-guide-art="{ONDEMAND_SETTINGS["tvc_guide_art"]}"'
        extinf_line += f',{channel_name}'
        m3u_content.extend([extinf_line, stream_url])
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')


@app.route('/')
def index(): return f"Roku Channels Bridge is running. <a href='/status'>View Status</a>"

@app.route('/remote')
def remote_control(): return render_template('remote.html')

@app.route('/preview')
def preview():
    all_channels = sorted(CHANNELS + EPG_CHANNELS, key=lambda x: x.get('name', '').lower())
    return render_template('preview.html', channels=all_channels)

@app.route('/pretune')
def pretune_page(): return render_template('pretune.html', ondemand_apps=ONDEMAND_APPS)

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
        'encoding_mode': ENCODING_MODE, 'audio_bitrate': AUDIO_BITRATE,
        'audio_channels': os.getenv('AUDIO_CHANNELS', '2'),
        'debug_logging': DEBUG_LOGGING_ENABLED, 'app_version': APP_VERSION
    }
    return render_template('status.html', global_settings=settings)

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        try:
            new_config = request.get_json()
            with open(CONFIG_FILE_PATH, 'w') as f: json.dump(new_config, f, indent=2)
            load_config()
            os.kill(os.getppid(), signal.SIGHUP)
            return jsonify({"message": "Configuration saved. Server is reloading."})
        except Exception as e: return jsonify({"error": str(e)}), 500
    else:
        try:
            with open(CONFIG_FILE_PATH, 'r') as f: config_data = json.load(f)
            return jsonify(config_data)
        except FileNotFoundError:
            return jsonify({ "tuners": [], "channels": [], "epg_channels": [], "ondemand_apps": [], "ondemand_settings": {}, "tmdb_api_key": "", "channels_dvr_ip": "" })
        except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/upload_config', methods=['POST'])
def upload_config():
    if 'file' not in request.files: return "No file part", 400
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.json'): return "Invalid file", 400
    try:
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
    if file.filename == '' or not file.filename.endswith('_plugin.py'): return "Invalid file", 400
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
        return f"Error saving plugin file: {e}", 500
        
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

@app.route('/api/plugins')
def api_plugins():
    plugin_list = [{"id": script_name, "name": plugin.app_name} for script_name, plugin in discovered_plugins.items()]
    return jsonify(plugin_list)

@app.route('/api/pretune/status')
def api_pretune_status():
    with SESSION_LOCK:
        active_ips = set(PREVIEW_SESSIONS.keys())
    status = []
    for tuner in TUNERS:
        tuner_status = "in-use" if tuner['in_use'] else "available"
        if tuner['roku_ip'] in active_ips:
            tuner_status = "pre-tuning"
            if tuner['roku_ip'] in RECORDING_TASKS:
                 tuner_status = "recording"
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
    return jsonify(result), 200 if result['status'] == 'success' else 503

@app.route('/api/pretune/stop', methods=['POST'])
def api_pretune_stop():
    tuner_ip = request.json.get('tuner_ip')
    if not tuner_ip: return jsonify({"status": "error", "message": "Tuner IP is required."}), 400
    return jsonify(stop_preview_session(tuner_ip))

@app.route('/api/pretune/commit', methods=['POST'])
def api_pretune_commit():
    data = request.get_json()
    tuner_ip = data.get('tuner_ip')
    record = data.get('record', False)
    duration = data.get('duration', 0)
    metadata = data.get('metadata', {})
    if not tuner_ip: return jsonify({"status": "error", "message": "Tuner IP is required."}), 400
    result = commit_preview_session(tuner_ip, record, duration, metadata)
    return jsonify(result), 200 if result['status'] == 'success' else 409
    
@app.route('/api/pretune/fetch_info', methods=['POST'])
def api_fetch_info():
    tuner_ip = request.json.get('tuner_ip')
    if not tuner_ip: return jsonify({"status": "error", "message": "Tuner IP is required."}), 400
    try:
        response = requests.get(f"http://{tuner_ip}:8060/query/media-player", timeout=3)
        response.raise_for_status()
        
        root = ET.fromstring(response.content)
        player_state = root.get('state')
        if not player_state or player_state == 'close':
            return jsonify({"status": "nodata"})

        media_node = root.find('.//plugin') or root.find('.//media')
        if media_node is None: return jsonify({"status": "nodata"})

        metadata = {}
        title = media_node.get('title')
        duration_str = media_node.get('duration')
        
        if title: metadata['title'] = title
        if duration_str:
            try: metadata['duration'] = round(float(duration_str) / 60)
            except ValueError: pass
        
        series_title = media_node.get('seriesTitle')
        episode_title = media_node.get('episodeTitle')
        if series_title and episode_title:
             metadata['title'] = series_title
             metadata['subtitle'] = episode_title

        if not metadata: return jsonify({"status": "nodata"})
        return jsonify({"status": "success", "metadata": metadata})

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch info from Roku {tuner_ip}: {e}")
        return jsonify({"status": "error", "message": "Could not connect to Roku."}), 500
    except ET.ParseError:
        logging.error(f"Failed to parse XML from Roku {tuner_ip}")
        return jsonify({"status": "nodata"})

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

@app.route('/api/status')
def api_status():
    statuses = []
    def check_tuner_status(tuner):
        roku_ip, encoder_url = tuner['roku_ip'], tuner['encoder_url']
        roku_status, encoder_status = 'offline', 'offline'
        try:
            roku_session.get(f"http://{roku_ip}:8060", timeout=3)
            roku_status = 'online'
        except requests.exceptions.RequestException: pass
        try:
            with requests.get(encoder_url, timeout=5, stream=True, allow_redirects=True) as response:
                response.raise_for_status()
                if next(response.iter_content(1), None): encoder_status = 'online'
        except requests.exceptions.RequestException: pass
        return { "name": tuner.get("name", roku_ip), "roku_ip": roku_ip, "encoder_url": encoder_url, "roku_status": roku_status, "encoder_status": encoder_status }

    with ThreadPoolExecutor(max_workers=len(TUNERS) or 1) as status_executor:
        statuses = list(status_executor.map(check_tuner_status, TUNERS))
    
    tuner_configs = [{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"], "encoder_url": t["encoder_url"]} for t in TUNERS]
    return jsonify({"tuners": tuner_configs, "statuses": statuses})
    
@app.route('/api/metadata/search', methods=['POST'])
def api_metadata_search():
    if not TMDB_API_KEY:
        return jsonify({"status": "error", "message": "TMDb API key is not configured."}), 400
    
    data = request.get_json()
    query = data.get('query')
    search_type = data.get('type', 'multi')
    if not query:
        return jsonify({"status": "error", "message": "Search query is required."}), 400

    try:
        url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={TMDB_API_KEY}&query={urllib.parse.quote(query)}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        results = response.json().get('results', [])
        
        formatted_results = []
        for item in results[:10]:
            media_type = item.get('media_type', search_type if search_type != 'multi' else 'movie')
            if media_type not in ['movie', 'tv']: continue

            title = item.get('title') or item.get('name')
            year = (item.get('release_date') or item.get('first_air_date') or 'N/A')[:4]
            poster_path = item.get('poster_path')
            
            formatted_results.append({
                "id": item.get('id'), "type": media_type, "title": title, "year": year,
                "poster": f"https://image.tmdb.org/t/p/w92{poster_path}" if poster_path else None
            })
        return jsonify({"status": "success", "results": formatted_results})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/metadata/details', methods=['POST'])
def api_metadata_details():
    if not TMDB_API_KEY:
        return jsonify({"status": "error", "message": "TMDb API key is not configured."}), 400
    
    data = request.get_json()
    media_id = data.get('id')
    media_type = data.get('type')
    if not media_id or not media_type:
        return jsonify({"status": "error", "message": "ID and type are required."}), 400

    try:
        url = f"https://api.themoviedb.org/3/{media_type}/{media_id}?api_key={TMDB_API_KEY}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        details = response.json()

        poster_path = details.get('poster_path')
        metadata = {
            "description": details.get('overview'),
            "image": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
        }
        return jsonify({"status": "success", "metadata": metadata})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ != '__main__':
    load_config()