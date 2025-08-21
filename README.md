# **Roku Channels Bridge**

**Release: Beta 2.0**

This project provides a Dockerized bridge that integrates your Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and uses this script to manage channel changes and proxy the video stream.

This setup allows you to use streaming service channels (like those from YouTube TV, Philo, etc.) just like traditional cable channels inside the Channels app.

## **Key Features**

* **Seamless Integration:** Adds Roku-based channels directly into your Channels DVR guide.

* **Dual M3U Support:** Generates two separate M3U playlists—one optimized for **Gracenote** guide data and another for custom **XMLTV/EPG** data.

* **Web-Based Management:** A built-in **Status Page** to monitor your devices and upload your configuration file.

* **Remote Control:** A web-based **Remote** to control any of your configured Roku devices from a browser on your phone, tablet, or computer.

* **Flexible Streaming Modes:** Choose between proxy, remux, or an efficient audio-only reencode mode to ensure stream stability with minimal CPU usage. **Can be set per-tuner.**

* **Hardware Acceleration:** Automatically detects and uses NVIDIA (NVENC) or Intel (QSV) GPUs for video processing if available.

* **Persistent Configuration:** Uses a Docker volume to safely store your configuration, so it persists through container updates and restarts.

## **Installation**

The application is distributed as a multi-architecture Docker image, ready to run.

### **Step 1: Pull the Docker Image**

Open a terminal or PowerShell and pull the latest image from Docker Hub.

```
docker pull rcvaughn2/roku-ecp-tuner

```

### **Step 2: Run the Docker Container**

Run the container using the command below. This command creates a persistent Docker volume named `roku-bridge-config` where your `roku_channels.json` file will be safely stored. It is configured to run with a single worker to ensure configuration changes are applied immediately without a container restart.

```
docker run -d \
  --name roku-channels-bridge \
  -p 5006:5000 \
  -v roku-bridge-config:/app/config \
  --restart unless-stopped \
  rcvaughn2/roku-ecp-tuner

```

**Note on GPU Acceleration (Linux):** If you need hardware acceleration for the `reencode` mode, add the `--device=/dev/dri` flag to the `docker run` command.

### **Step 3: Configure Your Tuners & Channels**

1.  Open your web browser and navigate to the Status & Configuration Page:
    `http://<IP_OF_DOCKER_HOST>:5006/status`

2.  Use the intuitive web interface to:
    * **Add Your Tuners:** Click "Add Tuner" and fill in the details for each Roku and HDMI encoder pair.
    * **Add Your Channels:** Add your Gracenote and Custom EPG channels using the dedicated "Add Channel" buttons. The forms provide all required and optional fields.
    * **Save Changes:** Once you've added your hardware and channels, click the "Save All Changes" button at the bottom of the page.

The server will automatically reload with your new configuration, and the status of your tuners will be displayed. Your setup is now complete.

## **Usage**

### **Channels DVR Setup**

This bridge generates two M3U playlist files, allowing you to choose between Gracenote's guide data or your own custom EPG data. The URLs are conveniently displayed on the Status page.

* **Gracenote M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u`
* **Custom EPG M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/epg_channels.m3u`

To add a source:

1.  Open your Channels DVR server settings.
2.  Under "Sources," click "+ Add Source" and choose "Custom Channels."
3.  Enter the desired M3U URL from above and configure the options as needed.

### **Web Interface**

* **Status & Config Page:** `http://<IP_OF_DOCKER_HOST>:5006/status`
    * Monitor the online/offline status of your Rokus and encoders.
    * Add, edit, and delete all tuners and channels.
    * Download your configuration for backup or upload a file to restore.
    * Toggle a full-width view for easier management on large screens.

* **Remote Control:** `http://<IP_OF_DOCKER_HOST>:5006/remote`
    * A full-featured remote control for any Roku device listed in your configuration file.

## **Configuration File (roku_channels.json)**

While all settings can now be managed through the web interface, the configuration is stored in a `roku_channels.json` file. This section serves as a reference for the data structure, which is useful for understanding the backup files, please refer to the [**Official Configuration Guide**](https://tuner.ct.ws).

### **`tuners` Section**

* **`name`**: A friendly name for the device pair (e.g., "Roku 1").
* **`roku_ip`**: The IP address of the Roku device.
* **`encoder_url`**: The full URL of the video stream from the HDMI encoder.
* **`priority`**: Determines the order tuners are used (lower number = higher priority).
* **`encoding_mode`**: **(Optional)** Sets the stream handling mode for this specific tuner (`proxy`, `remux`, or `reencode`).

### **`channels` Section (for Gracenote)**

* **`id`**: A unique identifier (e.g., "yt_cbs").
* **`name`**: The display name of the channel (e.g., "CBS").
* **`roku_app_id`**: The application ID for the Roku app.
* **`deep_link_content_id`**: The specific content ID to deep link to the channel.
* **`media_type`**: `live`, `movie`, `episode`, or `series`.
* **`tvc_guide_stationid`**: The station ID for Channels DVR guide data (Gracenote ID).
* **`tune_delay`**: **(Optional)** Time in seconds to wait after tuning before streaming.

### **`epg_channels` Section (for Custom EPG)**

This section contains the same required keys as the `channels` section, plus numerous optional keys for detailed customization (e.g., `channel-number`, `tvg-logo`, `tvc-guide-art`, etc.), all of which can be managed in the web UI.

## **Advanced Settings (Environment Variables)**

### **Stream Handling Modes (ENCODING_MODE)**

Set a global, default stream handling mode by adding the `-e ENCODING_MODE=<mode>` flag to your `docker run` command. This is used for any tuner that does **not** have a specific `encoding_mode` set.

* **`proxy` (Default):** Directly proxies the stream from your encoder.
* **`remux`:** Uses `ffmpeg` to copy the audio/video into a new, clean container.
* **`reencode`:** Copies the video stream but re-encodes the audio.

### **Other Environment Variables**

* **`AUDIO_BITRATE`**: Set audio quality for `reencode` mode (e.g., `-e AUDIO_BITRATE=192k`).
* **`AUDIO_CHANNELS`**: Set the number of audio channels (e.g., `-e AUDIO_CHANNELS=5.1`).
* **`ENABLE_DEBUG_LOGGING`**: Set to `true` for detailed logs (e.g., `-e ENABLE_DEBUG_LOGGING=true`).
