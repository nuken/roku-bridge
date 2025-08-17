# **Roku Channels Bridge**

**Release: Beta 1.7**

This project provides a Dockerized bridge that integrates your Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and uses this script to manage channel changes and proxy the video stream.

This setup allows you to use streaming service channels (like those from YouTube TV, Philo, etc.) just like traditional cable channels inside the Channels app.

## **Key Features**

  * **Seamless Integration:** Adds Roku-based channels directly into your Channels DVR guide.
  * **Dual M3U Support:** Generates two separate M3U playlists—one optimized for **Gracenote** guide data and another for custom **XMLTV/EPG** data.
  * **Web-Based Management:** A built-in **Status Page** to monitor your devices and upload your configuration file.
  * **Remote Control:** A web-based **Remote** to control any of your configured Roku devices from a browser on your phone, tablet, or computer.
  * **Flexible Streaming Modes:** Choose between proxy, remux, or an efficient audio-only reencode mode to ensure stream stability with minimal CPU usage.
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

Run the container using the command below. This command creates a persistent Docker volume named `roku-bridge-config` where your `roku_channels.json` file will be safely stored.

```
docker run -d \
  --name roku-channels-bridge \
  -p 5006:5000 \
  -v roku-bridge-config:/app/config \
  --restart unless-stopped \
  rcvaughn2/roku-ecp-tuner
```

**Note on GPU Acceleration (Linux):** If you need hardware acceleration for the `reencode` mode, add the `--device=/dev/dri` flag to the `docker run` command.

### **Step 3: Configure Your Tuners**

1.  Open your web browser and navigate to the Status Page:
    `http://<IP_OF_DOCKER_HOST>:5006/status`
2.  The page will show that no tuners are configured.
3.  In the **Update Configuration** section, click **"Choose File"** and select your prepared `roku_channels.json` file from your computer.
4.  Click **"Upload"**.

The page will confirm the upload was successful and will automatically refresh, showing the status of your newly configured tuners. Your setup is now complete.

## **Usage**

### **Channels DVR Setup**

This bridge generates two M3U playlist files, allowing you to choose between Gracenote's guide data or your own custom EPG data. You can add one or both as sources in Channels DVR.

  * **Gracenote M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u`
  * **Custom EPG M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/epg_channels.m3u`

To add a source:

1.  Open your Channels DVR server settings.
2.  Under "Sources," click "+ Add Source" and choose "Custom Channels."
3.  Enter the desired M3U URL from above and configure the options as needed.

### **Web Interface**

  * **Status Page:** `http://<IP_OF_DOCKER_HOST>:5006/status`
      * Monitor the online/offline status of your Rokus and encoders.
      * Upload a new `roku_channels.json` file at any time.
  * **Remote Control:** `http://<IP_OF_DOCKER_HOST>:5006/remote`
      * A full-featured remote control for any Roku device listed in your configuration file.

## **Configuration (roku\_channels.json)**

This file is the heart of your setup. For a detailed walkthrough on how to find the required information (like `roku_app_id` and `deep_link_content_id`), please refer to the [**Official Configuration Guide**](https://nuken.ct.ws/tuner).

The file is a JSON document split into three main sections: `tuners`, `channels`, and `epg_channels`.

### **`tuners` Section**

This is a list of your physical hardware setups (Roku + HDMI Encoder).

  * **`name`**: A friendly name for the device pair (e.g., "Living Room Roku").
  * **`roku_ip`**: The IP address of the Roku device.
  * **`encoder_url`**: The full URL of the video stream from the HDMI encoder.
  * **`priority`**: Determines the order tuners are used (lower number = higher priority).

**Example `tuners` section:**

```json
"tuners": [
  {
    "name": "Roku 1",
    "roku_ip": "192.168.1.10",
    "encoder_url": "http://192.168.1.20/ts/1_0",
    "priority": 1
  }
]
```

### **`channels` Section (for Gracenote)**

This list generates the `channels.m3u` file. Use this for channels where you want Channels DVR to automatically fetch guide data using the Gracenote station ID.

  * **`id`**: A unique identifier (e.g., "yt\_cbs").
  * **`name`**: The display name of the channel (e.g., "CBS").
  * **`roku_app_id`**: The application ID for the Roku app.
  * **`deep_link_content_id`**: The specific content ID to deep link to the channel.
  * **`media_type`**: Usually "live".
  * **`tvc_guide_stationid`**: **(Required)** The station ID for Channels DVR guide data (Gracenote ID).

**Example `channels` section:**

```json
"channels": [
  {
    "id": "yt_cbs",
    "name": "CBS",
    "roku_app_id": "20197",
    "deep_link_content_id": "some_youtube_tv_id",
    "media_type": "live",
    "tvc_guide_stationid": "12345"
  }
]
```

### **`epg_channels` Section (for Custom EPG)**

This list generates the `epg_channels.m3u` file. Use this for channels where you provide your own XMLTV guide data and want full control over the metadata.

#### **Required Keys**

  * **`id`**, **`name`**, **`roku_app_id`**, **`deep_link_content_id`**, **`media_type`**

#### **Optional Customization Keys**

Below is a list of all supported tags you can add to each channel in this section for detailed customization.

  * **`channel-number`**: Sets the channel number in the guide.
  * **`tvg-logo`**: URL or local path for the channel logo.
  * **`tvc-guide-art`**: URL or local path for the channel's background art.
  * **`tvc-guide-title`**: A custom title for the guide.
  * **`tvc-guide-description`**: A custom description for the guide.
  * **`tvc-guide-tags`**: Comma-separated list of tags (e.g., "HD,Sports").
  * **`tvc-guide-genres`**: Comma-separated list of genres (e.g., "Action,Adventure").
  * **`tvc-guide-categories`**: Comma-separated list of categories.
  * **`tvc-guide-placeholders`**: Used for automatic placeholders in Channels DVR.
  * **`tvc-stream-vcodec`**: Video codec information (e.g., "h264").
  * **`tvc-stream-acodec`**: Audio codec information (e.g., "aac").

**Note on Local Art:** For `tvg-logo` and `tvc-guide-art`, you can use both full URLs (`http://...`) and local paths to images already uploaded to Channels DVR (e.g., `"/dvr/uploads/102/content"`).

**Example `epg_channels` section:**

```json
"epg_channels": [
  {
    "id": "philo_cc",
    "name": "Comedy Central",
    "channel-number": "101.1",
    "tvg-logo": "https://my-logos.com/cc.png",
    "tvc-guide-art": "/dvr/uploads/115/content",
    "tvc-guide-genres": "Comedy",
    "roku_app_id": "196460",
    "deep_link_content_id": "Q2hhbm5lbDo2MDg1NDg4OTk2NDg0Mzg0OTk",
    "media_type": "live"
  }
]
```

## **Advanced Settings**

### **Stream Handling Modes (ENCODING\_MODE)**

Choose the right mode to balance performance and stability by adding the `-e ENCODING_MODE=<mode>` flag to your `docker run` command.

  * **`proxy` (Default):** Directly proxies the stream from your encoder. Best for clean, stable streams.
  * **`remux`:** Uses `ffmpeg` to copy the audio/video into a new, clean container. Fixes timing issues.
  * **`reencode`:** Copies the video stream as-is but re-encodes the audio. Fixes most common stream problems with minimal CPU impact.

### **Set Audio Bitrate (for `reencode` mode)**

To adjust audio quality, set the `AUDIO_BITRATE` environment variable (e.g., `-e AUDIO_BITRATE=192k`).

### **Enable Debug Logging**

To see detailed logs, add the `-e ENABLE_DEBUG_LOGGING=true` flag to your `docker run` command.
