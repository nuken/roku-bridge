import subprocess
import logging
import json
import os
import requests
import time
import threading
import httpx
import urllib.parse # Added for URL encoding
import signal # Added for Gunicorn reload
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, stream_with_context, render_template

# --- Import Plugin System ---
from plugins import discovered_plugins

app = Flask(__name__)

# --- Disable caching ---
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.config['TEMPLATES_AUTO_RELOAD'] = True

# --- Basic Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Environment & Global Variables ---
CONFIG_DIR = os.getenv('CONFIG_DIR', '/app/config')
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, 'roku_channels.json')
DEBUG_LOGGING_ENABLED = os.getenv('ENABLE_DEBUG_LOGGING', 'false').lower() == 'true'
ENCODING_MODE = os.getenv('ENCODING_MODE', 'proxy').lower()
AUDIO_BITRATE = os.getenv('AUDIO_BITRATE', '128k')
# A silent, empty MPEG-TS packet to keep the connection alive
SILENT_TS_PACKET = b'\x47\x40\x11\x10\x00\x02\xb0\x0d\x00\x01\xc1\x00\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff'

def get_audio_channels():
    channels_input = os.getenv('AUDIO_CHANNELS', '2').lower()
    if channels_input == "5.1": return '6'
    if channels_input == "7.1": return '8'
    return channels_input

AUDIO_CHANNELS = get_audio_channels()


# --- State Management for Tuner Pool ---
TUNERS = []
CHANNELS = []
EPG_CHANNELS = []
TUNER_LOCK = threading.Lock()
ENCODER_SETTINGS = {}

# Create persistent HTTP session for Roku commands
roku_session = requests.Session()
roku_session.timeout = 3
roku_session.headers.update({'Connection': 'keep-alive'})

# Thread pool for concurrent operations
executor = ThreadPoolExecutor(max_workers=4)

# --- Core Application Logic ---

def get_encoder_options():
    """Detects available ffmpeg hardware acceleration."""
    if DEBUG_LOGGING_ENABLED: logging.info("Detecting hardware acceleration...")
    try:
        result = subprocess.run(['ffmpeg', '-encoders'], capture_output=True, text=True, check=True)
        if 'h264_nvenc' in result.stdout:
            if DEBUG_LOGGING_ENABLED: logging.info("NVIDIA NVENC detected.")
            return {"codec": "h264_nvenc", "preset_args": ['-preset', 'p2'], "hwaccel_args": []}
        if 'h264_qsv' in result.stdout:
            if DEBUG_LOGGING_ENABLED: logging.info("Intel QSV detected.")
            return {"codec": "h264_qsv", "preset_args": [], "hwaccel_args": ['-hwaccel', 'qsv', '-c:v', 'h264_qsv']}
        if DEBUG_LOGGING_ENABLED: logging.info("No hardware acceleration found.")
        return {"codec": "libx264", "preset_args": ['-preset', 'superfast'], "hwaccel_args": []}
    except Exception as e:
        logging.error(f"ffmpeg detection failed: {e}. Defaulting to software.")
        return {"codec": "libx264", "preset_args": ['-preset', 'superfast'], "hwaccel_args": []}

def load_config():
    """Loads tuner and channel configuration."""
    global TUNERS, CHANNELS, EPG_CHANNELS
    if not os.path.exists(CONFIG_FILE_PATH):
        logging.warning(f"Config file not found at {CONFIG_FILE_PATH}. Creating default.")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w') as f:
                json.dump({"tuners": [], "channels": [], "epg_channels": []}, f, indent=2)
        except Exception as e:
            logging.error(f"Could not create default config: {e}")
            TUNERS, CHANNELS, EPG_CHANNELS = [], [], []
            return

    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            config_data = json.load(f) or {}
        TUNERS = sorted(config_data.get('tuners', []), key=lambda x: x.get('priority', 99))
        for tuner in TUNERS: tuner['in_use'] = False
        CHANNELS = config_data.get('channels', [])
        EPG_CHANNELS = config_data.get('epg_channels', [])
        if DEBUG_LOGGING_ENABLED: logging.info(f"Loaded {len(TUNERS)} tuners, {len(CHANNELS)} Gracenote channels, {len(EPG_CHANNELS)} EPG channels.")
    except Exception as e:
        logging.error(f"Error loading config: {e}")
        TUNERS, CHANNELS, EPG_CHANNELS = [], [], []

def lock_tuner():
    """Finds and locks an available tuner."""
    with TUNER_LOCK:
        for tuner in TUNERS:
            if not tuner.get('in_use'):
                tuner['in_use'] = True
                if DEBUG_LOGGING_ENABLED: logging.info(f"Locked tuner: {tuner.get('name')}")
                return tuner
    return None

def release_tuner(tuner_ip):
    """Releases a locked tuner and sends a 'Home' command."""
    with TUNER_LOCK:
        for tuner in TUNERS:
            if tuner.get('roku_ip') == tuner_ip:
                tuner['in_use'] = False
                if DEBUG_LOGGING_ENABLED: logging.info(f"Released tuner: {tuner.get('name')}")
                try:
                    roku_session.post(f"http://{tuner_ip}:8060/keypress/Home")
                except requests.exceptions.RequestException as e:
                    if DEBUG_LOGGING_ENABLED: logging.warning(f"Failed to send 'Home' to {tuner_ip}: {e}")
                break

def send_key_sequence(device_ip, keys):
    """Sends a sequence of keypresses to a device."""
    for key in keys:
        try:
            if isinstance(key, dict) and 'wait' in key:
                time.sleep(float(key['wait']))
                continue
            
            safe_key = f"Lit_{urllib.parse.quote(key)}" if len(key) == 1 else key
            roku_session.post(f"http://{device_ip}:8060/keypress/{safe_key}")
            if DEBUG_LOGGING_ENABLED: logging.info(f"Sent key '{key}' to {device_ip}")
            time.sleep(0.5) # Delay between keys
        except Exception as e:
            logging.error(f"Failed to send key '{key}' to {device_ip}: {e}")
            return False
    return True

def execute_tuning_in_background(roku_ip, channel_data):
    """The main tuning logic, designed to run in a background thread."""
    try:
        if DEBUG_LOGGING_ENABLED: logging.info(f"Tuning to actual channel {channel_data['name']}...")
        
        # 1. Launch the app
        launch_url = f"http://{roku_ip}:8060/launch/{channel_data['roku_app_id']}"
        roku_session.post(launch_url)
        
        # 2. Wait for app to load
        tune_delay = channel_data.get("tune_delay", 1)
        time.sleep(tune_delay)

        # 3. Determine and execute the tuning method
        plugin_script = channel_data.get('plugin_script')
        key_sequence = channel_data.get('key_sequence')

        if plugin_script and plugin_script in discovered_plugins:
            plugin = discovered_plugins[plugin_script]
            final_sequence = plugin.tune_channel(roku_ip, channel_data)
            if final_sequence:
                send_key_sequence(roku_ip, final_sequence)

        elif key_sequence:
            send_key_sequence(roku_ip, key_sequence)

        else: # Deep Linking
            content_id = channel_data.get('deep_link_content_id')
            if content_id:
                media_type = channel_data.get('media_type', 'live')
                params = f"?contentId={content_id}&mediaType={media_type}"
                if DEBUG_LOGGING_ENABLED: logging.info(f"Sending deep link command: {launch_url}{params}")
                roku_session.post(f"{launch_url}{params}")
        
        # 4. Final 'Select' keypress if needed
        if channel_data.get('needs_select_keypress'):
            time.sleep(1)
            send_key_sequence(roku_ip, ["Select"])

    except Exception as e:
        logging.error(f"Error during background tuning for {roku_ip}: {e}")

@app.route('/upload_splash', methods=['POST'])
def upload_splash():
    if 'file' not in request.files:
        return "No file part in the request.", 400
    file = request.files['file']
    if file.filename == '':
        return "No file selected for uploading.", 400
    
    save_path = os.path.join(CONFIG_DIR, 'splash.ts')
    
    try:
        file.save(save_path)
        if DEBUG_LOGGING_ENABLED: logging.info(f"Splash screen saved to {save_path}")
        return "Splash screen uploaded successfully!", 200
    except Exception as e:
        logging.error(f"Error saving splash screen: {e}")
        return f"Error saving file: {e}", 500

def stream_generator(encoder_url, roku_ip_to_release, mode='proxy', blank_duration=0):
    """
    A generator that handles all streaming modes.
    If blank_duration is > 0, it first streams silent TS packets for that duration.
    """
    try:
        # Step 1: Stream silent packets for the blanking duration
        if blank_duration > 0:
            if DEBUG_LOGGING_ENABLED: logging.info(f"Starting silent stream for {blank_duration} seconds...")
            start_time = time.time()
            while time.time() - start_time < blank_duration:
                yield SILENT_TS_PACKET
                time.sleep(0.1) # Send a packet every 100ms
            if DEBUG_LOGGING_ENABLED: logging.info("Finished silent stream.")

        # Step 2: Switch to the main stream
        if DEBUG_LOGGING_ENABLED: logging.info(f"Switching to live stream from encoder ({mode} mode)...")
        if mode in ['remux', 'reencode']:
            command = ['ffmpeg', '-i', encoder_url]
            if mode == 'reencode':
                command.extend(['-c:v', 'copy', '-c:a', 'aac', '-b:a', AUDIO_BITRATE, '-ac', AUDIO_CHANNELS])
            else:
                command.extend(['-c', 'copy'])
            command.extend(['-f', 'mpegts', '-loglevel', 'error', '-'])
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for chunk in iter(lambda: process.stdout.read(8192), b''):
                yield chunk
            process.wait()
        else: # Proxy
            with httpx.stream("GET", encoder_url, timeout=15, follow_redirects=True) as r:
                for chunk in r.iter_bytes():
                    yield chunk
    except Exception as e:
        logging.error(f"Stream error for {roku_ip_to_release} ({mode}): {e}")
    finally:
        release_tuner(roku_ip_to_release)

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    locked_tuner = lock_tuner()
    if not locked_tuner: return "All tuners are in use.", 503

    channel_data = next((c for c in CHANNELS + EPG_CHANNELS if c["id"] == channel_id), None)
    if not channel_data:
        release_tuner(locked_tuner['roku_ip'])
        return "Channel not found.", 404

    executor.submit(execute_tuning_in_background, locked_tuner['roku_ip'], channel_data)

    tuner_mode = locked_tuner.get('encoding_mode', ENCODING_MODE)
    blank_duration = channel_data.get('blank_duration', 0)
    generator = stream_generator(locked_tuner['encoder_url'], locked_tuner['roku_ip'], tuner_mode, blank_duration)
    
    return Response(stream_with_context(generator), mimetype='video/mpeg')

@app.route('/api/plugins')
def get_plugins():
    return jsonify([{"id": name, "name": plugin.app_name} for name, plugin in discovered_plugins.items()])

def generate_m3u_from_channels(channel_list):
    """Generic M3U generator."""
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    for channel in channel_list:
        stream_url = f"http://{request.host}/stream/{channel['id']}"
        extinf_line = f'#EXTINF:-1 channel-id="{channel["id"]}"'
        tags = { "tvg-name": "name", "channel-number": "channel-number", "tvg-logo": "tvg-logo", "tvc-guide-title": "tvc-guide-title",
            "tvc-guide-description": "tvc-guide-description", "tvc-guide-art": "tvc-guide-art", "tvc-guide-tags": "tvc-guide-tags",
            "tvc-guide-genres": "tvc-guide-genres", "tvc-guide-categories": "tvc-guide-categories", "tvc-guide-placeholders": "tvc-guide-placeholders",
            "tvc-stream-vcodec": "tvc-stream-vcodec", "tvc-stream-acodec": "tvc-stream-acodec", "tvc-guide-stationid": "tvc_guide_stationid" }
        for tag, key in tags.items():
            if key in channel: extinf_line += f' {tag}="{channel[key]}"'
        extinf_line += f',{channel["name"]}'
        m3u_content.extend([extinf_line, stream_url])
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

@app.route('/channels.m3u')
def generate_gracenote_m3u():
    return generate_m3u_from_channels(CHANNELS)

@app.route('/epg_channels.m3u')
def generate_epg_m3u():
    return generate_m3u_from_channels(EPG_CHANNELS)

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

@app.route('/')
def index():
    return f"Roku Channels Bridge is running. <a href='/status'>View Status</a> | <a href='/remote'>Go to Remote</a>"

@app.route('/remote')
def remote_control():
    return render_template('remote.html')

@app.route('/remote/devices')
def get_remote_devices():
    return jsonify([{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"]} for t in TUNERS])

@app.route('/remote/keypress/<device_ip>/<key>', methods=['POST'])
def remote_keypress(device_ip, key):
    if not any(t['roku_ip'] == device_ip for t in TUNERS): return jsonify({"status": "error", "message": "Device not found."}), 404
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

@app.route('/status')
def status_page():
    settings = { 'encoding_mode': ENCODING_MODE, 'audio_bitrate': AUDIO_BITRATE,
                 'audio_channels': os.getenv('AUDIO_CHANNELS', '2'), 'debug_logging': DEBUG_LOGGING_ENABLED }
    return render_template('status.html', global_settings=settings)

@app.route('/api/config', methods=['GET'])
def get_config():
    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            config_data = json.load(f)
        return jsonify(config_data)
    except FileNotFoundError:
        return jsonify({"tuners": [], "channels": [], "epg_channels": []})
    except Exception as e:
        logging.error(f"Error reading config file for API: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/config', methods=['POST'])
def update_config():
    try:
        new_config = request.get_json()
        if not all(k in new_config for k in ['tuners', 'channels', 'epg_channels']):
            return jsonify({"error": "Invalid configuration structure."}), 400
        with open(CONFIG_FILE_PATH, 'w') as f: json.dump(new_config, f, indent=2)
        load_config()
        os.kill(os.getppid(), signal.SIGHUP)
        return jsonify({"message": "Configuration saved successfully. Server is reloading."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        except requests.exceptions.RequestException:
            pass

        try:
            with requests.get(encoder_url, timeout=5, stream=True, allow_redirects=True) as response:
                response.raise_for_status()
                if next(response.iter_content(1), None):
                    encoder_status = 'online'
        except requests.exceptions.RequestException:
            pass

        return {
            "name": tuner.get("name", roku_ip), "roku_ip": roku_ip, "encoder_url": encoder_url,
            "roku_status": roku_status, "encoder_status": encoder_status
        }

    with ThreadPoolExecutor(max_workers=len(TUNERS) or 1) as status_executor:
        statuses = list(status_executor.map(check_tuner_status, TUNERS))

    tuner_configs = [{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"], "encoder_url": t["encoder_url"]} for t in TUNERS]

    return jsonify({"tuners": tuner_configs, "statuses": statuses})

if __name__ != '__main__':
    load_config()
    ENCODER_SETTINGS = get_encoder_options()