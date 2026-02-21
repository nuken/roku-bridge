import logging
import json
import os
import requests
import threading
import urllib.parse
from flask import Flask, request, jsonify, Response, stream_with_context, render_template, redirect

app = Flask(__name__)
APP_VERSION = "5.0.0-LEAN"

# Strict Proxy-Only Configuration
CONFIG_DIR = os.getenv('CONFIG_DIR', '/app/config')
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR, 'roku_channels.json')

# Global State
TUNERS, CHANNELS = [], []
TUNER_LOCK = threading.Lock()
roku_session = requests.Session()
roku_session.timeout = 5 # Faster timeout for high-performance encoders

def load_config():
    global TUNERS, CHANNELS
    # Ensure the directory exists
    os.makedirs(CONFIG_DIR, exist_ok=True)

    if not os.path.exists(CONFIG_FILE_PATH):
        logging.info(f"Config file not found. Creating default at {CONFIG_FILE_PATH}")
        with open(CONFIG_FILE_PATH, 'w') as f:
            json.dump({"tuners": [], "channels": []}, f, indent=2)

    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            config_data = json.load(f) or {}
            TUNERS = config_data.get('tuners', [])
            CHANNELS = config_data.get('channels', [])
            # Reset tuner status for fresh start
            for tuner in TUNERS: tuner['in_use'] = False
    except Exception as e:
        logging.error(f"Error loading config: {e}")

def execute_fast_tune(roku_ip, channel_data):
    """Uses the ECP Launch endpoint for instantaneous deep-linking."""
    content_id = channel_data.get('deep_link_content_id')
    app_id = channel_data.get('roku_app_id')

    if content_id and app_id:
        # The correct direct deep-link format for Roku ECP
        url = f"http://{roku_ip}:8060/launch/{app_id}?contentId={content_id}&mediaType=live"
        try:
            logging.info(f"Tuning {roku_ip} to app {app_id} with content {content_id}")
            roku_session.post(url, timeout=2)
        except Exception as e:
            logging.error(f"Tuning failed on {roku_ip}: {e}")

def stream_generator(encoder_url, roku_ip, tuner):
    """Pure proxy generator optimized for LinkPi hardware."""
    try:
        # timeout=(5, 60) means 5s to connect, but waits up to 60s for video chunks
        with requests.get(encoder_url, timeout=(5, 60), stream=True) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
    except requests.exceptions.ReadTimeout:
        logging.warning(f"Stream from {encoder_url} timed out. The LinkPi may have stopped sending data.")
    except Exception as e:
        logging.error(f"Streaming Error from {encoder_url}: {e}")
    finally:
        # Release tuner and return to Home to clear the Roku player
        try:
            requests.post(f"http://{roku_ip}:8060/keypress/Home", timeout=2)
        except Exception:
            pass # Ignore cleanup errors
        
        with TUNER_LOCK:
            tuner['in_use'] = False
            logging.info(f"Released tuner at {roku_ip}")

@app.route('/')
def index():
    return redirect('/status')

@app.route('/status')
def status_page():
    settings = {
        'app_version': APP_VERSION
    }
    return render_template('status.html', global_settings=settings)

# CRITICAL FIX: The API endpoint required for status.html to work
@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'POST':
        try:
            new_config = request.get_json()
            # Clean up IPs just in case
            if 'tuners' in new_config:
                for t in new_config['tuners']:
                    t['roku_ip'] = t['roku_ip'].replace('http://', '').replace('https://', '').strip()

            with open(CONFIG_FILE_PATH, 'w') as f:
                json.dump(new_config, f, indent=2)

            load_config()
            return jsonify({"message": "Configuration saved"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"tuners": TUNERS, "channels": CHANNELS})

@app.route('/remote')
def remote_control():
    return render_template('remote.html')

# FIX: API endpoints required for remote.html to actually control the Roku
@app.route('/remote/devices')
def get_remote_devices():
    return jsonify([{"name": t.get("name", t["roku_ip"]), "roku_ip": t["roku_ip"]} for t in TUNERS])

@app.route('/remote/keypress/<device_ip>/<key>', methods=['POST'])
def remote_keypress(device_ip, key):
    try:
        roku_session.post(f"http://{device_ip}:8060/keypress/{urllib.parse.quote(key)}")
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stream/<channel_id>')
def stream_channel(channel_id):
    # Tuner Locking Logic
    with TUNER_LOCK:
        tuner = next((t for t in TUNERS if not t.get('in_use')), None)
        if not tuner: return "No Tuners Available", 503
        tuner['in_use'] = True

    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        with TUNER_LOCK:
            tuner['in_use'] = False
        return "Channel Not Found", 404

    # Execute Search Launch
    threading.Thread(target=execute_fast_tune, args=(tuner['roku_ip'], channel)).start()

    # Pass the tuner object to the generator so it can be unlocked later
    return Response(stream_with_context(stream_generator(tuner['encoder_url'], tuner['roku_ip'], tuner)),
                    mimetype='video/mpeg')

@app.route('/channels.m3u')
def generate_m3u():
    # Grab the playlist filter from the URL, if it exists
    requested_playlist = request.args.get('playlist')
    
    m3u_content = [f"#EXTM3U x-tvh-max-streams={len(TUNERS)}"]
    for channel in CHANNELS:
        # If a URL filter is applied, skip channels that do not match
        if requested_playlist and channel.get('playlist') != requested_playlist:
            continue
            
        stream_url = f"http://{request.host}/stream/{channel['id']}"
        # Gracenote integration: tvc-guide-stationid allows Channels DVR to auto-match guide data
        extinf = f'#EXTINF:-1 channel-id="{channel["id"]}" tvc-guide-stationid="{channel.get("gracenote_id", "")}"'

        if 'playlist' in channel and channel['playlist']:
            extinf += f' group-title="{channel["playlist"]}"'

        extinf += f',{channel["name"]}'
        m3u_content.extend([extinf, stream_url])

    return Response("\n".join(m3u_content), mimetype='audio/x-mpegurl')
    
@app.route('/preview/<channel_id>')
def preview_channel(channel_id):
    channel = next((c for c in CHANNELS if c["id"] == channel_id), None)
    if not channel:
        return "Channel Not Found", 404
    return render_template('preview.html', channel=channel)

# IMPORTANT: Ensure this is called at the bottom of the script
if __name__ == 'app' or __name__ == '__main__':
    load_config()