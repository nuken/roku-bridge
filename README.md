# **Roku Channels Bridge \- Version 0.02.0**

This project provides a Dockerized bridge server that allows you to use one or more Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and using this script to manage channel changes and proxy the video stream.

This version now **auto-detects available GPU hardware** (Intel QSV or NVIDIA NVENC) to handle the demanding video re-encoding process. This significantly reduces CPU usage. If no compatible GPU is found, it falls back to a CPU-efficient software encode.

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
