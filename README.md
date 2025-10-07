# **Roku Channels Bridge**

**Release: Beta 4.5**

[**Official Configuration Guide**](https://tuner.ct.ws)

This project provides a bridge that integrates your Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and uses this script to manage channel changes and proxy the video stream.

This setup allows you to use streaming service channels (like those from YouTube TV, Philo, etc.) and on-demand apps (like Max and Netflix) just like traditional cable channels inside the Channels app.

## **Key Features**

  * **Seamless Integration:** Adds Roku-based channels directly into your Channels DVR guide.
  * **On-Demand App Streaming:** A dedicated **Pre-Tuning** page allows you to launch any on-demand app, navigate to your content, and send the final video stream directly to Channels DVR, solving stream delay issues.
  * **Advanced Tuning Methods:** Supports direct **Deep Linking**, custom **Key-Sequence** tuning, and an extensible **Plugin System** for apps that require complex navigation.
  * **Hide the Tuning Process:** Use the **Blanking Duration** feature to show a black screen while the Roku tunes in the background, providing a seamless, professional viewing experience.
  * **Triple M3U Support:** Generates three separate M3U playlists—one for **Gracenote**, one for custom **XMLTV/EPG** data, and a new one for **On-Demand Apps**.
  * **Web-Based Management:** A built-in **Status Page** to monitor your devices and manage your entire configuration with an intuitive UI.
  * **Remote Control:** A web-based **Remote** to control any of your configured Roku devices from a browser on your phone, tablet, or computer.
  * **Flexible Streaming Modes:** Choose between `proxy`, `remux`, or an efficient audio-only `reencode` mode to ensure stream stability with minimal CPU usage. This can be set per-tuner.
  * **Hardware Acceleration:** Automatically detects and uses NVIDIA (NVENC) or Intel (QSV) GPUs for video processing if available.
  * **Persistent Configuration:** Uses a Docker volume to safely store your configuration, so it persists through container updates and restarts.
  * **Roku as a Tuner**: Exposes your Roku devices as live TV streams.
  * **On-Demand Pre-Tuning**: A web UI to navigate Roku apps, start content, and then initiate a local recording, ensuring a clean start without recording menus or ads.
  * **Local Recording**: Record directly from your HDMI encoder to a local folder, bypassing Channels DVR for recordings.
  * **Metadata Integration**: Automatically embeds metadata (title, summary) into recorded files and saves artwork for seamless integration with media servers like Plex or Jellyfin.
  * **Extensible Plugin System**: Supports custom Python plugins to automate complex tuning sequences for different apps.
  
## **Installation**

The application is distributed as a multi-architecture Docker image, ready to run.

### 1. Run with Docker

The easiest way to run the application is with Docker Compose.

1.  Create a folder for your project.
2.  Inside that folder, create a subfolder named `recordings`. This is where your movies and TV shows will be saved.
3.  Create a file named `docker-compose.yml` in the main folder and add the following content:

    ```yaml
    version: '3.7'

    services:
      roku-bridge:
        image: rcvaughn2/roku-ecp-tuner:stream
        container_name: roku-channels-bridge
        ports:
          - "5006:5000" # Host port : Container port
        volumes:
          - roku-bridge-config:/app/config
          - ./recordings:/app/recordings
        environment:
          - ENABLE_DEBUG_LOGGING=true
        restart: unless-stopped

    volumes:
      roku-bridge-config:
    ```

4.  Open a terminal in your project folder and run: `docker-compose up -d`


**Note on GPU Acceleration (Linux):** If you need hardware acceleration for the `reencode` mode, add the `--device=/dev/dri` flag to the `docker run` command.

### **Step 3: Configure Your Tuners & Channels**

Once the container is running, open your web browser and navigate to `http://<your-ip>:5006/status` to access the configuration panel.

- **Tuners**: Add each of your Roku devices by providing a name, its IP address, and the URL of its corresponding HDMI encoder stream.
- **Integrations**: Add your TMDb API key to enable online metadata searching.

## Using the On-Demand Pre-Tuning Feature

This feature is designed to give you perfect, clean recordings of on-demand content from any Roku app.

1.  **Navigate to Pre-Tune**: Open `http://<your-ip>:5006/pretune`.

2.  **Start a Session**: Select an available tuner from the list and click "Start". A live video preview from the Roku will appear.

3.  **Select Mode**:
    - **Watch Mode (Default)**: For live viewing. Use the on-screen remote to navigate to a channel or app. Once content is playing, click **"Send to Channels DVR"** to get a stream URL you can use in any player.
    - **Record Mode**: Click the **"Record"** button to reveal the recording options.

4.  **Prepare Your Recording**:
    - Use the remote to navigate to the movie or show you want to record and get it to the playback screen.
    - Use the "Fetch from Roku" or "Search Online" buttons to populate the metadata fields (Title, Summary, etc.).
    - Select the "Content-Type" (Movie or TV Show) to ensure the file is saved in the correct subfolder.
    - The "Duration" will be auto-filled from the online search. You can add extra time using the "Padding" field.

5.  **Start Recording**: Once your content is ready, click **"Start Local Recording"**. The application will begin recording the stream directly to your local `recordings` folder.

## **Usage**

### **Channels DVR Setup**

This bridge generates three M3U playlist files. The URLs are conveniently displayed on the Status page.

  * **Gracenote M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u`
  * **Custom EPG M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/epg_channels.m3u`
  * **On-Demand M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/ondemand.m3u`

To add a source, open your Channels DVR server settings, go to "Sources," click "+ Add Source," choose "Custom Channels," and enter the desired M3U URL.

### **Using the Playlist Feature (Optional)**

You can organize your live TV channels into smaller, more manageable groups by using the playlist feature. This allows you to generate M3U files that only contain a specific subset of your channels.

1.  **Assign Channels to a Playlist:**

      * On the Status & Config page, edit a Gracenote or EPG channel.
      * In the **"Playlist Name"** field, enter a name (e.g., `YTTV`, `Philo`, `Sports`).
      * Save the channel and repeat for all other channels you want in that group.

2.  **Generate a Filtered M3U URL:**

      * To get an M3U file for only the channels in a specific playlist, add `?playlist=<playlist_name>` to the end of the standard M3U URL.
      * The playlist name is **case-sensitive** and must exactly match what you entered in the channel settings.

    **Examples:**

      * `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u?playlist=YTTV`
      * `http://<IP_OF_DOCKER_HOST>:5006/epg_channels.m3u?playlist=Philo`

    You can add each filtered URL as a separate "Custom Channels" source in Channels DVR, making it easier to manage large numbers of channels.

### **Web Interface**

  * **Status & Config Page:** `http://<IP_OF_DOCKER_HOST>:5006/status`
      * Monitor the online/offline status of your Rokus and encoders.
      * Add, edit, and delete all tuners, channels, and on-demand apps.
      * Download or upload your configuration file.
  * **Remote Control:** `http://<IP_OF_DOCKER_HOST>:5006/remote`
      * A full-featured remote for any configured Roku.
  * **On-Demand Pre-Tuning:** `http://<IP_OF_DOCKER_HOST>:5006/pretune`
      * A "Mission Control" page to launch on-demand apps and send them to Channels DVR.

## **Using the On-Demand Pre-Tuning Feature**

This feature allows you to stream content from any non-live TV app (like Max, Netflix, Hulu, etc.) to Channels DVR. It solves the issue of stream delays by letting you prepare the content *before* sending it to your DVR.

1.  **Add Your Apps:** On the main Status page, add the on-demand apps you want to use.
2.  **Open the Pre-Tune Page:** Navigate to `http://<IP_OF_DOCKER_HOST>:5006/pretune`. The page will automatically lock the first available tuner and start a live video preview.
3.  **Launch an App:** Select an app from the dropdown menu to launch it on the Roku.
4.  **Navigate and Play:** Use the on-screen remote controls (or the full remote on a desktop) to navigate the app and start playing the movie or show you want to watch.
5.  **Send to Channels:** Once the content is playing in the preview window, click the **"Send to Channels DVR"** button.
6.  **Tune In:** Go to your Channels DVR app and tune to the "On-Demand Stream" channel. The content you selected will begin playing from the start.

## **Advanced Tuning Methods (For Live TV)**

For apps that don't support direct deep-linking, you have two powerful options.

### **1. Key Sequence Tuning**

Define a sequence of remote control commands to navigate to the correct channel after an app has launched.

  * **`Tune Delay`**: Pauses the script after launching the app to give it time to load *before* sending commands. (Default: 1s)
  * **`Blanking Duration`**: Shows a black screen for a set number of seconds to hide the tuning process from view.

### **2. Plugin System**

For even more complex tuning logic, you can use app-specific Python plugins.

## **Configuration File (`roku_channels.json`)**

While all settings can be managed through the web interface, the configuration is stored in a `roku_channels.json` file.

### **`tuners` Section**

  * **`name`**: A friendly name for the device pair.
  * **`roku_ip`**: The IP address of the Roku device.
  * **`encoder_url`**: The full URL of the video stream from the HDMI encoder.

### **`ondemand_apps` Section**

```json
"ondemand_apps": [
    {
      "name": "Max",
      "id": "max_app",
      "roku_app_id": "8378"
    },
    {
      "name": "Netflix",
      "id": "netflix_app",
      "roku_app_id": "13"
    }
]
```
