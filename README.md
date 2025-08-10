# **Roku Channels Bridge**

**Release: Beta 1.2**

This project provides a Dockerized bridge that integrates your Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and uses this script to manage channel changes and proxy the video stream.

This setup allows you to use streaming service channels (like those from YouTube TV, Philo, etc.) just like traditional cable channels inside the Channels app.

## **Key Features**

  * **Seamless Integration:** Adds Roku-based channels directly into your Channels DVR guide.
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

1.  Open your Channels DVR server settings.
2.  Under "Sources," click "Add Source" and choose "Custom Channels."
3.  Set the following values:
      * **Stream Format:** MPEG-TS
      * **URL:** `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u`
4.  Save the source. Channels will automatically import your channels and download the guide data.

### **Web Interface**

  * **Status Page:** `http://<IP_OF_DOCKER_HOST>:5006/status`
      * Monitor the online/offline status of your Rokus and encoders.
      * Upload a new `roku_channels.json` file at any time.
  * **Remote Control:** `http://<IP_OF_DOCKER_HOST>:5006/remote`
      * A full-featured remote control for any Roku device listed in your configuration file.

## **Configuration (roku\_channels.json)**

This file is the heart of your setup. For a detailed walkthrough on how to find the required information (like `roku_app_id` and `deep_link_content_id`), please refer to the [**Official Configuration Guide**](https://codetricks.ct.ws/roku).

The file is a JSON document split into two main sections: `tuners` and `channels`.

### **`tuners` Section**

This is a list of your physical hardware setups (Roku + HDMI Encoder).

  * **`name`**: A friendly name for the device pair (e.g., "Living Room Roku").
  * **`roku_ip`**: The IP address of the Roku device.
  * **`encoder_url`**: The full URL of the video stream from the HDMI encoder.
  * **`priority`**: Determines the order tuners are used (lower number = higher priority).

**Example `tuners` section:**

```
"tuners": [
  {
    "name": "Roku 1",
    "roku_ip": "192.168.1.10",
    "encoder_url": "http://192.168.1.20/ts/1_0",
    "priority": 1
  },
  {
    "name": "Roku 2",
    "roku_ip": "192.168.1.11",
    "encoder_url": "rtsp://192.168.1.21:554/stream",
    "priority": 2
  }
]
```

### **`channels` Section**

This is a list of all the channels you want to make available.

  * **`id`**: A unique, simple identifier for the channel (e.g., "philo\_cc").
  * **`name`**: The display name of the channel (e.g., "Comedy Central").
  * **`roku_app_id`**: The application ID for the Roku app.
  * **`deep_link_content_id`**: The specific content ID to deep link to the channel.
  * **`media_type`**: Usually "live".
  * **`tvc_guide_stationid`**: The station ID for Channels DVR guide data (Gracenote ID).

#### **Optional Channel Settings**

You can add these keys to any channel for more control:

  * **`tune_delay`**: (Number) Seconds to wait after tuning before starting the stream. Useful for apps with splash screens. Defaults to 3.
  * **`needs_select_keypress`**: (Boolean) Set to `true` if the app requires an "OK/Select" press to start the stream after deep linking.

**Example `channels` section:**

```
"channels": [
  {
    "id": "philo_cc",
    "name": "Comedy Central",
    "roku_app_id": "196460",
    "deep_link_content_id": "Q2hhbm5lbDo2MDg1NDg4OTk2NDg0Mzg0OTk",
    "media_type": "live",
    "tvc_guide_stationid": "10149"
  },
  {
    "id": "yt_cbs",
    "name": "CBS",
    "roku_app_id": "20197",
    "deep_link_content_id": "some_youtube_tv_id",
    "media_type": "live",
    "tvc_guide_stationid": "12345",
    "tune_delay": 5
  }
]
```

## **Advanced Settings**

### **Stream Handling Modes (ENCODING\_MODE)**

Choose the right mode to balance performance and stability by adding the `-e ENCODING_MODE=<mode>` flag to your `docker run` command.

  * **`proxy` (Default):**
      * **CPU Usage:** Very Low
      * **Description:** Directly proxies the stream from your encoder. Best for clean, stable streams.
  * **`remux`:**
      * **CPU Usage:** Very Low
      * **Description:** Uses `ffmpeg` to copy the audio/video into a new, clean container. Fixes timing issues but not corrupted data.
  * **`reencode`:**
      * **CPU Usage:** Low
      * **Description:** **(Recommended for fixing issues)** Copies the video stream as-is but re-encodes the audio. This fixes most common stream problems (like audio corruption) with minimal CPU impact.

### **Enable Debug Logging**

To see detailed logs for troubleshooting, add the `-e ENABLE_DEBUG_LOGGING=true` flag to your `docker run` command.
