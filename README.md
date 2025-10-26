# **Roku Channels Bridge**

**Release: Beta 5.1.1**

[**Official Configuration Guide**](https://tuner.ct.ws)

This project provides a bridge that integrates your Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and uses this script to manage channel changes and proxy the video stream.

This setup allows you to use streaming service channels (like those from YouTube TV, Philo, etc.) and on-demand apps (like Max and Netflix) just like traditional cable channels inside the Channels app.

## **Key Features**

  * **Seamless Integration:** Adds Roku-based channels directly into your Channels DVR guide.
  * **Intelligent On-Demand Recording:** A dedicated **Pre-Tuning** page allows you to stage content from any on-demand app, start recording with the press of a button, and have the recording automatically stop when the content finishes or if the next episode starts to auto-play.
  * **Advanced Tuning Methods:** Supports direct **Deep Linking**, custom **Key-Sequence** tuning, and an extensible **Plugin System** for apps that require complex navigation.
  * **Hide the Tuning Process:** Use the **Blanking Duration** feature to show a black screen while the Roku tunes in the background, providing a seamless, professional viewing experience.
  * **Triple M3U Support:** Generates three separate M3U playlists—one for **Gracenote**, one for custom **XMLTV/EPG** data, and a new one for **On-Demand Apps**.
  * **Web-Based Management:** A built-in **Status Page** to monitor your devices and manage your entire configuration with an intuitive UI.
  * **Remote Control:** A web-based **Remote** to control any of your configured Roku devices from a browser on your phone, tablet, or computer.
  * **Metadata Integration**: Automatically searches TMDb, embeds metadata (title, summary) into recorded files, and saves artwork for seamless integration with media servers like Plex, Jellyfin, or Channels DVR.
  * **Flexible Streaming Modes:** Choose between `proxy`, `remux`, or an efficient audio-only `reencode` mode to ensure stream stability with minimal CPU usage. This can be set per-tuner.
  * **Hardware Acceleration:** Automatically detects and uses NVIDIA (NVENC) or Intel (QSV) GPUs for video processing if available.
  * **Persistent Configuration:** Uses a Docker volume to safely store your configuration, so it persists through container updates and restarts.

-----

## **Installation (Recommended: Portainer)**

The easiest and recommended way to run the application is using Portainer.

### Step 1: Create the Volume in Portainer

This volume stores your configuration and ensures it persists through container updates.

1.  In Portainer, go to **Volumes** in the left menu.
2.  Click **+ Add volume**.
3.  Name the volume exactly `roku-bridge-config`.
4.  Click **Create the volume**.

### Step 2: Deploy the Stack in Portainer

1.  Go to **Stacks** in the left menu.

2.  Click **+ Add stack**.

3.  Give your stack a name (e.g., `roku-bridge`).

4.  Select the **Web editor** and paste the following `docker-compose.yml` code:

    ```yaml
    version: '3.8'

    services:
      roku-bridge:
        # The official Docker image for the project.
        image: rcvaughn2/roku-ecp-tuner:test
        container_name: roku-bridge
        ports:
          # You can change the host port (e.g., "5006") if it conflicts with another service.
          - "5006:5000"
        volumes:
          # This volume stores the application's configuration files.
          - roku-bridge-config:/app/config
          # IMPORTANT: Change the path on the left to your desired local storage location for recordings.
          - /path/to/your/recordings:/app/recordings
        restart: unless-stopped

    volumes:
      # This tells Docker Compose to use the volume you created in Step 1.
      roku-bridge-config:
        external: true
    ```

### Step 3: Set Your Recordings Path (Crucial)

Before deploying, you **must** edit one line in the web editor to set your local recordings path.

  * **Find this line:**
    `- /path/to/your/recordings:/app/recordings`

  * **Change `/path/to/your/recordings`** to the absolute path on your host machine where you want your recordings saved.

      * **Example (Linux):** `- /srv/data/roku-recordings:/app/recordings`
      * **Example (Windows):** `- C:/Users/YourUser/Videos/RokuRecordings:/app/recordings`
      * **Example (Synology):** `- /volume1/docker/roku-bridge/recordings:/app/recordings`

### Step 4: Deploy and Configure

1.  Click the **Deploy the stack** button.
2.  Once the container is running, open your web browser and navigate to `http://<your-ip>:5006/status` to access the configuration panel.

-----

### **Alternative Installation: Docker Compose CLI**

For users who prefer the command line:

1.  Create a folder for your project (e.g., `mkdir roku-bridge` and `cd roku-bridge`).

2.  Create the Docker volume manually first:

    ```sh
    docker volume create roku-bridge-config
    ```

3.  Create a file named `docker-compose.yml` and add the following content.

    ```yaml
    version: '3.8'

    services:
      roku-bridge:
        image: rcvaughn2/roku-ecp-tuner:test
        container_name: roku-bridge
        ports:
          - "5006:5000" # Host port : Container port
        volumes:
          - roku-bridge-config:/app/config
          # Change the path on the left to your host's recording folder
          - /path/to/your/recordings:/app/recordings
        restart: unless-stopped

    volumes:
      roku-bridge-config:
        external: true
    ```

    **Note:** Remember to change `/path/to/your/recordings` to your local path.

4.  Open a terminal in your project folder and run: `docker-compose up -d`

-----

## **Post-Install Configuration**

Once the container is running, open the web UI at `http://<your-ip>:5006/status`.

1.  **Tuners**: Add each of your Roku devices by providing a name, its IP address, and the URL of its corresponding HDMI encoder stream.
2.  **Integrations**: To enable automatic metadata and artwork lookup for your recordings, you need a free API key from **The Movie Database (TMDb)**.
      * Register for a free account at [https://www.themoviedb.org/signup](https://www.themoviedb.org/signup).
      * In your account settings, go to the **API** section and request a key.
      * Copy the **API Key (v3 auth)** and paste it into the "TMDb API Key" field in the "Integrations" section of the config page.
3.  Click **Save All Changes**.

-----

## **Usage**

### Using the On-Demand Recording Feature

This feature is designed to give you perfect, clean recordings of on-demand content from any Roku app.

1.  **Navigate to Pre-Tune**: Open `http://<your-ip>:5006/pretune`.

2.  **Start a Session**: Select an available tuner from the list and click "Start". A live video preview from the Roku will appear.

3.  **Select Record Mode**: Click the **Record** button to reveal the recording options.

4.  **Stage Your Content (Most Important Step)**:

      * Use the remote to navigate to the movie or show you want to record.
      * Start playing the content, and then immediately **pause it** at the exact moment you want the recording to begin (e.g., right after the studio logos).

5.  **Fill in Metadata**:

      * Select the "Content-Type" (Movie or TV Show).
      * Type the title into the "Title" field and click **"Search Online"**.
      * Click the correct result from the list to automatically fill in the description, duration, and artwork.

6.  **Start Recording**: Once your content is paused and ready, click **"Start Local Recording"**. The application will automatically send the "Play" command to the Roku and begin recording. The recording will stop automatically when the content finishes.

### Channels DVR Setup

This bridge generates three M3U playlist files. The URLs are conveniently displayed on the Status page.

  * **Gracenote M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u`
  * **Custom EPG M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/epg_channels.m3u`
  * **On-Demand M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/ondemand.m3u`

To add a source for live channels, open your Channels DVR server settings, go to "Sources," click "+ Add Source," choose "Custom Channels," and enter the desired M3U URL.

### Integrating Local Recordings with Channels DVR

To view your recordings in Channels DVR, add the `recordings` subfolders as "Personal Media".

**Note**: The `Movies` and `TV Shows` folders will not be created until you make at least one recording of each type.

1.  Make your first movie and/or TV show recording.
2.  In the Channels DVR Server web UI, go to **Settings** -\> **Sources**.
3.  Click **Add Source** -\> **Personal Media**.
4.  **Add Movies**:
      * Name: `Recorded Movies`
      * Path: Navigate to your `recordings` folder (the one you defined in your `docker-compose.yml`) and select the `Movies` subfolder.
      * Content Type: **Movies**
5.  **Add TV Shows**:
      * Click **Add Source** -\> **Personal Media** again.
      * Name: `Recorded TV Shows`
      * Path: Navigate to your `recordings` folder and select the `TV Shows` subfolder.
      * Content Type: **TV Shows**

Channels DVR will now scan these folders and import your recordings with all the metadata and artwork.
