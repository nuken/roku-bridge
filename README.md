# **Roku Channels Bridge**

**Release: Beta 3.0**

This project provides a Dockerized bridge that integrates your Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and uses this script to manage channel changes and proxy the video stream.

This setup allows you to use streaming service channels (like those from YouTube TV, Philo, etc.) just like traditional cable channels inside the Channels app.

## **Key Features**

  * **Seamless Integration:** Adds Roku-based channels directly into your Channels DVR guide.
  * **Advanced Tuning Methods:** Supports direct **Deep Linking**, custom **Key-Sequence** tuning, and an extensible **Plugin System** for apps that require complex navigation.
  * **Hide the Tuning Process:** Use the **Blanking Duration** feature to show a black screen while the Roku tunes in the background, providing a seamless, professional viewing experience.
  * **Dual M3U Support:** Generates two separate M3U playlists—one optimized for **Gracenote** guide data and another for custom **XMLTV/EPG** data.
  * **Web-Based Management:** A built-in **Status Page** to monitor your devices and manage your entire configuration with an intuitive UI.
  * **Remote Control:** A web-based **Remote** to control any of your configured Roku devices from a browser on your phone, tablet, or computer.
  * **Flexible Streaming Modes:** Choose between `proxy`, `remux`, or an efficient audio-only `reencode` mode to ensure stream stability with minimal CPU usage. This can be set per-tuner.
  * **Hardware Acceleration:** Automatically detects and uses NVIDIA (NVENC) or Intel (QSV) GPUs for video processing if available.

## **Installation**

The application is distributed as a multi-architecture Docker image, ready to run.

### **Step 1: Pull the Docker Image**

Open a terminal or PowerShell and pull the latest image from Docker Hub.

```
docker pull rcvaughn2/roku-ecp-tuner:test
```

### **Step 2: Run the Docker Container**

Run the container using the command below. This command creates a persistent Docker volume named `roku-bridge-config` where your `roku_channels.json` and plugin files will be safely stored.

```
docker run -d  \
--name roku-channels-bridge  \
-p 5006:5000  \
-v roku-bridge-config:/app/config  \
--restart unless-stopped  \
rcvaughn2/roku-ecp-tuner:test
```

**Note on GPU Acceleration (Linux):** If you need hardware acceleration for the `reencode` mode, add the `--device=/dev/dri` flag to the `docker run` command.

### **Step 3: Configure Your Tuners & Channels**

1.  Open your web browser and navigate to the Status & Configuration Page:
    `http://<IP_OF_DOCKER_HOST>:5006/status`
2.  Use the intuitive web interface to:
      * **Add Your Tuners:** Click "Add Tuner" and fill in the details for each Roku and HDMI encoder pair.
      * **Add Your Channels:** Add your Gracenote and Custom EPG channels using the dedicated "Add Channel" buttons. The forms provide all required and optional fields for deep linking, key sequences, and plugins.
      * **Save Changes:** Once you've added your hardware and channels, click the "Save All Changes" button at the bottom of the page.

The server will automatically reload with your new configuration, and the status of your tuners will be displayed. Your setup is now complete.

## **Usage**

### **Channels DVR Setup**

This bridge generates two M3U playlist files. The URLs are conveniently displayed on the Status page.

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
  * **Remote Control:** `http://<IP_OF_DOCKER_HOST>:5006/remote`
      * A full-featured remote control for any Roku device listed in your configuration file.

## **Advanced Tuning Methods**

For apps that don't support direct deep-linking, you have two powerful options.

### **1. Key Sequence Tuning**

Define a sequence of remote control commands to navigate to the correct channel after an app has launched.

  * **`Tune Delay`**: Pauses the script after launching the app to give it time to load *before* sending commands. (Default: 1s)
  * **`Blanking Duration`**: Shows a black screen for a set number of seconds to hide the tuning process from view. This should be long enough to cover the Tune Delay and the entire key sequence.

#### **Available Commands**

  * **`Up`**, **`Down`**, **`Left`**, **`Right`**: Navigational arrow keys.
  * **`Select`**: The "OK" button.
  * **`wait=<seconds>`**: Pauses the sequence for a specific duration (e.g., `wait=2` for two seconds).

### **2. Plugin System**

For even more complex tuning logic, you can use app-specific Python plugins. A plugin allows you to define a custom tuning sequence in code, giving you maximum flexibility.

  * **`plugin_script`**: Select the plugin file to use for the channel.
  * **`plugin_data`**: A field for providing custom data to your plugin, such as a channel's position in a guide list.

To add a new plugin, simply create a new `_plugin.py` file in your `config/plugins` directory. The application will automatically detect and load it.

## **Configuration File (`roku_channels.json`)**

While all settings can be managed through the web interface, the configuration is stored in a `roku_channels.json` file. This section serves as a reference for the data structure.

### **`tuners` Section**

  * **`name`**: A friendly name for the device pair.
  * **`roku_ip`**: The IP address of the Roku device.
  * **`encoder_url`**: The full URL of the video stream from the HDMI encoder.
  * **`priority`**: Determines the order tuners are used (lower number = higher priority).
  * **`encoding_mode`**: **(Optional)** Sets the stream handling mode for this specific tuner (`proxy`, `remux`, or `reencode`).

### **`channels` Section (Example)**

```json
{
  "id": "my_custom_channel",
  "name": "My Channel",
  "roku_app_id": "12345",
  "tvc_guide_stationid": "67890",
  "media_type": "live",
  "tune_delay": 5,
  "blank_duration": 15,
  "key_sequence": [
    "Down",
    "wait=2",
    "Select"
  ]
},
{
      "id": "fox_one_wjzy",
      "media_type": "live",
      "name": "Fox 46",
      "plugin_data": {
        "list_position": 1
      },
      "plugin_script": "fox_one_plugin.py",
      "roku_app_id": "808732",
      "tune_delay": 6,
      "tvc_guide_stationid": "11594"
    }
```

## **Advanced Settings (Environment Variables)**

  * **`ENCODING_MODE`**: Set the global default stream handling mode (`proxy`, `remux`, `reencode`).
  * **`AUDIO_BITRATE`**: Set audio quality for `reencode` mode (e.g., `192k`).
  * **`AUDIO_CHANNELS`**: Set the number of audio channels (e.g., `5.1`).
  * **`ENABLE_DEBUG_LOGGING`**: Set to `true` for detailed logs.
