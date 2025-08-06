# **Roku Channels Bridge \- Version 0.03.4**

This project provides a Dockerized bridge server that allows you to use one or more Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and using this script to manage channel changes and proxy the video stream.

0.03.3 `ENCODING_MODE` environment variable now defaults to `proxy` mode and allows for switching to `ffmpeg` re-encoding mode by adding `-e ENCODING_MODE=reencode` flag to the run command. If your Channels DVR logs show `Packet corrupt` errors and the stream keeps stopping, you will need to use the `-e ENCODING_MODE=reencode` flag for testing.

0.03.4 Send a Home command to the Roku after tuner release.

## **How It Works**

1. **Channels DVR** requests a channel from the M3U playlist provided by this server.  
2. The **Bridge Server** receives the request and finds an available Roku/encoder pair from its pool.  
3. It sends an **ECP** deep link command to the Roku, telling it to tune to the correct app and channel.  
4. The server connects to the corresponding **HDMI encoder** and uses ffmpeg to re-encode the stream, using the best available hardware.  
5. The clean, re-encoded stream is sent back to Channels DVR for viewing or recording.

## **Setup and Installation**

### **Step 1: Download the Project Files**

1. On the main page of the GitHub repository, click the green **\< \> Code** button.  
2. In the dropdown menu, click **Download ZIP**.  
3. Extract the ZIP file to a folder on the computer where you run Docker.

### **Step 2: Configure roku\_channels.json**

This is the most important step. You must edit the roku\_channels.json file to match your specific hardware and channel lineup. The file is split into two sections: tuners and channels.

#### **tuners**

This is a list of your physical hardware setups (Roku \+ HDMI Encoder).

* name: A friendly name for the device pair.  
* roku\_ip: The IP address of the Roku device.  
* encoder\_url: The full URL of the video stream from the HDMI encoder.  
* priority: Determines the order tuners are used (lower number \= higher priority).

**Example tuners section:**

```

"tuners": [  
  {  
    "name": "Living Room Roku",  
    "roku_ip": "192.168.1.10",  
    "encoder_url": "http://192.168.1.20/ts/1_0",  
    "priority": 1  
  },  
  {  
    "name": "Bedroom Roku",  
    "roku_ip": "192.168.1.11",  
    "encoder_url": "rtsp://192.168.1.21:554/stream",  
    "priority": 2  
  }  
]

```

#### **channels**

This is a list of all the channels you want to make available.

* id: A unique, simple identifier for the channel.  
* name: The display name of the channel.  
* roku\_app\_id: The application ID for the Roku app.  
* deep\_link\_content\_id: The specific content ID to deep link to the channel.  
* media\_type: Usually "live".  
* tvc\_guide\_stationid: The station ID for Channels DVR guide data. (Gracenote ID)

**Example channels section:**

```

"channels": [  
  {  
    "id": "philo_cc",  
    "name": "Comedy Central",  
    "roku_app_id": "196460",  
    "deep_link_content_id": "Q2hhbm5lbDo2MDg1NDg4OTk2NDg0Mzg0OTk",  
    "media_type": "live",  
    "tvc_guide_stationid": "10149"  
  }  
]

```

### **Step 3: Build the Docker Image**

Open a terminal or PowerShell, navigate to the folder containing the project files, and run the following command.

```

docker build -t roku-channels-bridge .

```

### **Step 4: Run the Docker Container**

Run the container using the appropriate command below. This command maps a port to the container, mounts your config folder, and ensures it restarts automatically.

#### **A) Without GPU Acceleration (CPU Only)**

This is the simplest method and will use software encoding.

**For Windows (PowerShell/CMD):**

```

docker run -d --name roku-channels-bridge -p 5006:5000 -v C:\path\to\your\config-folder:/app/config --restart unless-stopped roku-channels-bridge

```

**For Linux / macOS:**

```

docker run -d --name roku-channels-bridge -p 5006:5000 -v /path/to/your/config-folder:/app/config --restart unless-stopped roku-channels-bridge

```

#### **B) With GPU Hardware Acceleration (Recommended)**

To allow the container to access your GPU, you must add an extra flag to the docker run command.

For Linux (Intel & NVIDIA):  
You need to pass the dri device to the container.  

```

docker run -d --name roku-channels-bridge -p 5006:5000 --device=/dev/dri -v /path/to/your/config-folder:/app/config --restart unless-stopped roku-channels-bridge

```

For Windows (Docker Desktop with WSL2):  
GPU passthrough should be handled automatically by Docker Desktop if your host drivers are correctly installed and WSL is updated. You do not need the \--device flag.

1. Ensure your Intel or NVIDIA graphics drivers are fully updated on your Windows host machine.  
2. Open PowerShell and run wsl \--update to ensure your WSL version is current.  
3. Run the standard command (from option A). The container will automatically detect the GPU if WSL is configured correctly.

**Note:** Replace C:\\path\\to\\your\\config-folder or /path/to/your/config-folder with the actual path to the folder on your host machine where your roku\_channels.json file is located.

## **Usage in Channels DVR**

1. Open your Channels DVR server settings.  
2. Under "Sources," click "Add Source" and choose "Custom Channels."  
3. Set the following values:  
   * **Stream Format:** MPEG-TS
   * **URL:** http://\<IP\_OF\_DOCKER\_HOST\>:5006/channels.m3u  
4. Save the source. Channels will automatically import your channels and download the guide data.



## Finding Your Roku's Information

To configure the bridge, you need three key pieces of information from your Roku device: its IP address, the app ID for each streaming service, and the specific content ID for each live channel.

### Step 1: Find Your Roku's IP Address

1.  On your Roku remote, press the **Home** button.
2.  Navigate to **Settings** \> **Network** \> **About**.
3.  Note the **IP Address** (e.g., `192.168.1.100`).

### Step 2: Enable Control by Mobile Apps

This setting is required for the script to send commands to the Roku.

1.  On your Roku remote, press the **Home** button.
2.  Navigate to **Settings** \> **System** \> **Advanced system settings**.
3.  Select **Control by mobile apps**.
4.  Ensure the "Network access" setting is set to **Default** or **Permissive**.

### Step 3: Find Roku App IDs

Run the following command in a terminal or PowerShell window, replacing `YOUR_ROKU_IP` with the IP address you found in Step 1.

```
bash
curl http://YOUR_ROKU_IP:8060/query/apps

```

This will return an XML list of all installed applications and their corresponding ID numbers. Find the apps you want to use (e.g., YouTube TV, Philo) and note their IDs.

*Finding the `deep_link_content_id` for each specific live channel is more complex and varies by app. This typically requires a more advanced network analysis while the app is running or locating it from the streaming services website player.*

## Updating the Configuration on a Running Container

If you need to add, remove, or change channels without rebuilding and restarting your Docker container, you can upload a modified `roku_channels.json` file directly to the running application.

This is done using a `curl` command from a terminal or PowerShell window on a computer on the same network.

#### **The Command**

```bash
curl -X POST -F "file=@roku_channels.json" http://<IP_OF_DOCKER_HOST>:<PORT>/upload_config
```

#### **Command Breakdown**

  * `curl`: A command-line tool for transferring data with URLs.
  * `-X POST`: Specifies that you are making a `POST` request, which is used to send data to a server.
  * `-F "file=@roku_channels.json"`: This tells `curl` to send the data as a form.
      * `file=`: This corresponds to the field name the server is expecting.
      * `@roku_channels.json`: The `@` symbol is crucial. It tells `curl` to read the content of the file named `roku_channels.json` from your current directory and send that content as the data.
  * `http://<IP_OF_DOCKER_HOST>:<PORT>/upload_config`: This is the destination URL.
      * You must replace `<IP_OF_DOCKER_HOST>` with the IP address of the machine running your Docker container.
      * Replace `<PORT>` with the port you mapped in your `docker run` command (e.g., `5006`).

#### **How to Use It: Step-by-Step**

1.  **Modify Your File:** Make any desired changes to your local `roku_channels.json` file and save it.
2.  **Open a Terminal:** Open PowerShell, Command Prompt, or a terminal on your computer.
3.  **Navigate to the Folder:** Use the `cd` command to navigate to the directory where your modified `roku_channels.json` file is located.
    ```powershell
    # Example
    cd C:\Users\Bobby\Documents\N\roku-channels-bridge
    ```
4.  **Run the Command:** Execute the `curl` command, ensuring the IP address and port are correct for your setup.
    ```bash
    curl -X POST -F "file=@roku_channels.json" http://192.168.86.64:5006/upload_config
    ```

If the upload is successful, the server will respond with a JSON message like:
`{"status":"success","message":"Configuration updated successfully"}`

The application will immediately reload the new configuration and use it for all subsequent stream requests.


## Optional Channel Settings

You can add the following optional keys to any channel in the `channels` list of your `roku_channels.json` file to fine-tune its behavior.

---

### Custom Tuning Delay

Some Roku apps, like YouTube TV, have a splash screen that displays before the video stream begins. The `tune_delay` key allows you to set a custom wait time (in seconds) for a specific channel, ensuring the script doesn't start capturing the stream too early.

If this key is omitted, a default delay of 3 seconds will be used.

**Example:**
```json
{
  "id": "yt_cbs_east",
  "name": "CBS (East)",
  "tvc_guide_stationid": "12345",
  "roku_app_id": "20197",
  "deep_link_content_id": "some_youtube_tv_id",
  "media_type": "live",
  "tune_delay": 4
}
```
### Guide Data Time Zone Shift

If a channel's guide data doesn't match your local time zone (e.g., you are watching a West Coast feed in an East Coast time zone), you can use the guide_shift key to apply an offset. The value is in seconds.To shift the guide back 1 hour, use -3600.To shift the guide forward 1 hour, use 3600.If this key is omitted, no time shift will be applied.

**Example:**
```json
{
  "id": "philo_cc_west",
  "name": "Comedy Central (West)",
  "tvc_guide_stationid": "67890",
  "roku_app_id": "196460",
  "deep_link_content_id": "some_philo_id",
  "media_type": "live",
  "guide_shift": -10800
}
```
### Required "Select" Keypress

Certain apps may require a "Select" command to be sent after the initial deep link to start the video stream. By adding "needs_select_keypress": true, you can tell the script to perform this extra step.If this key is omitted, no extra keypress will be sent.

**Example:**
```json
{
  "id": "sa_cbs_east",
  "name": "CBS (East)",
  "tvc_guide_stationid": "12345",
  "roku_app_id": "20197",
  "deep_link_content_id": "some_app_id",
  "media_type": "live",
  "needs_select_keypress": true
}
```

## Enable Logging 

1.  The script now checks for a new environment variable called `ENABLE_DEBUG_LOGGING.`

2.  By default, this is off, and the logs will remain clean, only showing critical errors.

3.  If a user starts the container with `-e ENABLE_DEBUG_LOGGING=true`, all the detailed operational logs (tuner locking, ffmpeg commands, etc.) will be printed, which is perfect for debugging.

## Enable Re-encoding

If you have stream breaking, add the `-e ENCODING_MODE=reencode` flag to the run command.
