import subprocess
import logging
import json
import os
import requests
import time
import threading
import httpx
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, Response, stream_with_context, render_template

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


# --- State Management for Tuner Pool ---
TUNERS = []
CHANNELS = []
EPG_CHANNELS = [] # New list for EPG channels
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
    """Loads tuner and channel configuration. Creates a default if not found."""
    global TUNERS, CHANNELS, EPG_CHANNELS
    if not os.path.exists(CONFIG_FILE_PATH):
        logging.warning(f"Config file not found at {CONFIG_FILE_PATH}. Creating a default empty config.")
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE_PATH, 'w') as f:
                json.dump({"tuners": [], "channels": [], "epg_channels": []}, f, indent=2)
        except Exception as e:
            logging.error(f"Could not create default config file: {e}")
            TUNERS, CHANNELS, EPG_CHANNELS = [], [], []
            return

    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            content = f.read()
            if not content:
                config_data = {"tuners": [], "channels": [], "epg_channels": []}
            else:
                config_data = json.loads(content)

        TUNERS = sorted(config_data.get('tuners', []), key=lambda x: x.get('priority', 99))
        for tuner in TUNERS:
            tuner['in_use'] = False
        CHANNELS = config_data.get('channels', [])
        EPG_CHANNELS = config_data.get('epg_channels', []) # Load epg_channels
        if DEBUG_LOGGING_ENABLED: logging.info(f"Loaded {len(TUNERS)} tuners, {len(CHANNELS)} Gracenote channels, and {len(EPG_CHANNELS)} EPG channels.")
    except (json.JSONDecodeError, Exception) as e:
        logging.error(f"Error loading config file: {e}. It might be empty or corrupted.")
        TUNERS, CHANNELS, EPG_CHANNELS = [], [], []

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

                def send_home_async():
                    try:
                        home_url = f"http://{tuner_ip}:8060/keypress/Home"
                        roku_session.post(home_url)
                        if DEBUG_LOGGING_ENABLED:
                            logging.info(f"Sent 'Home' command to Roku at {tuner_ip}")
                    except requests.exceptions.RequestException as e:
                        if DEBUG_LOGGING_ENABLED:
                            logging.warning(f"Failed to send 'Home' command to Roku at {tuner_ip}: {e}")

                executor.submit(send_home_async)
                break

def tune_roku_async(roku_ip, tune_url):
    """Tune Roku in a separate thread to reduce blocking."""
    try:
        response = roku_session.post(tune_url)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to tune Roku at {roku_ip}: {e}")
        return False

def send_additional_commands(roku_ip, channel_data):
    """Send additional commands (Select) after tuning delay."""
    try:
        if channel_data.get('needs_select_keypress'):
            select_url = f"http://{roku_ip}:8060/keypress/Select"
            roku_session.post(select_url)
            if DEBUG_LOGGING_ENABLED:
                logging.info(f"Sent Select keypress to {roku_ip}")
            time.sleep(0.5)

    except requests.exceptions.RequestException as e:
        if DEBUG_LOGGING_ENABLED:
            logging.warning(f"Failed to send additional commands to {roku_ip}: {e}")

# --- Streaming Functions ---

def reencode_stream_generator(encoder_url, roku_ip_to_release):
    """Generator for a more optimized ffmpeg method.
    It copies the video stream and re-encodes only the audio.
    This is much less CPU-intensive than a full re-encode."""
    try:
        command = [
            'ffmpeg',
            '-analyzeduration', '1M',
            '-probesize', '1M',
            '-err_detect', 'ignore_err',
            '-fflags', '+genpts',
            '-i', encoder_url,
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-b:a', AUDIO_BITRATE,
            '-f', 'mpegts',
            '-loglevel', 'error',
            '-'
        ]
        if DEBUG_LOGGING_ENABLED:
            logging.info(f"Starting FFMPEG RE-ENCODE (Audio Only) for tuner {roku_ip_to_release}")
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
            '-analyzeduration', '1M',
            '-probesize', '1M',
            '-i', encoder_url,
            '-c', 'copy',
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
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=True) as client:
            with client.stream("GET", encoder_url) as r:
                r.raise_for_status()
                for data in r.iter_bytes():
                    yield data
    except Exception as e:
        logging.error(f"Error in proxy_stream_generator for {roku_ip_to_release}: {e}")
    finally:
        release_tuner(roku_ip_to_release)

# --- Flask Routes ---

def generate_m3u_from_channels(channel_list):
    """Generic M3U generator."""
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    for channel in channel_list:
        stream_url = f"http://{request.host}/stream/{channel['id']}"
        extinf_line = f'#EXTINF:-1 channel-id="{channel["id"]}"'

        tags_to_add = {
            "tvg-name": "name",
            "channel-number": "channel-number",
            "tvg-logo": "tvg-logo",
            "tvc-guide-title": "tvc-guide-title",
            "tvc-guide-description": "tvc-guide-description",
            "tvc-guide-art": "tvc-guide-art",
            "tvc-guide-tags": "tvc-guide-tags",
            "tvc-guide-genres": "tvc-guide-genres",
            "tvc-guide-categories": "tvc-guide-categories",
            "tvc-guide-placeholders": "tvc-guide-placeholders",
            "tvc-stream-vcodec": "tvc-stream-vcodec",
            "tvc-stream-acodec": "tvc-stream-acodec",
            "tvc-guide-stationid": "tvc_guide_stationid" # Corrected typo here
        }

        for tag, key in tags_to_add.items():
            if key in channel:
                extinf_line += f' {tag}="{channel[key]}"'
        
        extinf_line += f',{channel["name"]}'
        
        m3u_content.append(extinf_line)
        m3u_content.append(stream_url)
        
    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')

@app.route('/channels.m3u')
def generate_gracenote_m3u():
    return generate_m3u_from_channels(CHANNELS)

@app.route('/epg_channels.m3u')
def generate_epg_m3u():
    return generate_m3u_from_channels(EPG_CHANNELS)

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    start_time = time.time()
    locked_tuner = lock_tuner()
    if not locked_tuner:
        return "All tuners are currently in use.", 503

    # Look for the channel in both lists
    channel_data = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel_data:
        channel_data = next((c for c in EPG_CHANNELS if c["id"] == channel_id), None)

    if not channel_data:
        release_tuner(locked_tuner['roku_ip'])
        return "Channel not found.", 404

    roku_ip = locked_tuner['roku_ip']

    try:
        base_url = f"http://{roku_ip}:8060/launch/{channel_data['roku_app_id']}"
        content_id = channel_data['deep_link_content_id']
        media_type = channel_data['media_type']

        if '=' in content_id or '&' in content_id:
            roku_tune_url = f"{base_url}?{content_id}&mediaType={media_type}"
        else:
            roku_tune_url = f"{base_url}?contentId={content_id}&mediaType={media_type}"
        
        if DEBUG_LOGGING_ENABLED:
            logging.info(f"Constructed tune URL: {roku_tune_url}")

        tune_future = executor.submit(tune_roku_async, roku_ip, roku_tune_url)

        if not tune_future.result(timeout=5):
            release_tuner(roku_ip)
            return "Failed to tune Roku", 500

        tune_delay = channel_data.get("tune_delay", 3)

        if channel_data.get('needs_select_keypress'):
            def delayed_commands():
                time.sleep(tune_delay)
                send_additional_commands(roku_ip, channel_data)
            executor.submit(delayed_commands)
        
        time.sleep(tune_delay)

        if DEBUG_LOGGING_ENABLED:
            total_time = time.time() - start_time
            logging.info(f"Total tuning time for {channel_id}: {total_time:.2f} seconds")

    except Exception as e:
        release_tuner(roku_ip)
        return f"Failed to tune Roku: {e}", 500

    if ENCODING_MODE == 'reencode':
        stream_generator = reencode_stream_generator(locked_tuner['encoder_url'], roku_ip)
    elif ENCODING_MODE == 'remux':
        stream_generator = remux_stream_generator(locked_tuner['encoder_url'], roku_ip)
    else:
        stream_generator = proxy_stream_generator(locked_tuner['encoder_url'], roku_ip)

    return Response(stream_with_context(stream_generator), mimetype='video/mpeg')

@app.route('/upload_config', methods=['POST'])
def upload_config():
    if 'file' not in request.files: return "No file part", 400
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.json'): return "Invalid file", 400
    try:
        file.save(CONFIG_FILE_PATH)
        load_config()
        return "Configuration updated successfully. Refreshing...", 200
    except Exception as e:
        return f"Error processing config file: {e}", 400

@app.route('/')
def index():
    return f"Roku Channels Bridge is running. <a href='/status'>View Status</a> | <a href='/remote'>Go to Remote</a>"

# --- Remote Control Routes ---

@app.route('/remote')
def remote_control():
    return render_template('remote.html')

@app.route('/remote/devices')
def get_remote_devices():
    remote_devices = [{"name": tuner.get("name", tuner["roku_ip"]), "roku_ip": tuner["roku_ip"]} for tuner in TUNERS]
    return jsonify(remote_devices)

@app.route('/remote/keypress/<device_ip>/<key>', methods=['POST'])
def remote_keypress(device_ip, key):
    if not any(tuner['roku_ip'] == device_ip for tuner in TUNERS):
        return jsonify({"status": "error", "message": "Device not found."}), 404
    try:
        roku_session.post(f"http://{device_ip}:8060/keypress/{key}")
        return jsonify({"status": "success"})
    except requests.exceptions.RequestException as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- Status Page Routes ---

@app.route('/status')
def status_page():
    return render_template('status.html')

@app.route('/api/status')
def api_status():
    statuses = []

    def check_tuner_status(tuner):
        roku_ip = tuner['roku_ip']
        encoder_url = tuner['encoder_url']

        try:
            roku_session.get(f"http://{roku_ip}:8060", timeout=2)
            roku_status = 'online'
        except requests.exceptions.RequestException:
            roku_status = 'offline'

        try:
            response = requests.head(encoder_url, timeout=2, allow_redirects=True)
            response.raise_for_status()
            encoder_status = 'online'
        except requests.exceptions.RequestException:
            try:
                response = requests.get(encoder_url, timeout=2, stream=True)
                response.raise_for_status()
                encoder_status = 'online'
            except requests.exceptions.RequestException:
                encoder_status = 'offline'

        return {
            "name": tuner.get("name", roku_ip),
            "roku_ip": roku_ip,
            "encoder_url": encoder_url,
            "roku_status": roku_status,
            "encoder_status": encoder_status
        }

    with ThreadPoolExecutor(max_workers=len(TUNERS) or 1) as status_executor:
        status_futures = [status_executor.submit(check_tuner_status, tuner) for tuner in TUNERS]
        statuses = [future.result() for future in status_futures]

    tuner_configs = [{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"], "encoder_url": t["encoder_url"]} for t in TUNERS]

    return jsonify({"tuners": tuner_configs, "statuses": statuses})

# --- App Initialization ---
if __name__ != '__main__':
    load_config()
    ENCODER_SETTINGS = get_encoder_options()
