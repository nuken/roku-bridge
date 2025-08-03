# **Roku Channels Bridge \- Version 0.01.0**

This project provides a Dockerized bridge server that allows you to use one or more Roku devices as tuners within the Channels DVR software. It works by capturing the HDMI output from a Roku with a dedicated HDMI encoder and using this script to manage channel changes and proxy the video stream.

This initial version focuses on **stability and reliability**. To achieve this, the application uses **ffmpeg** to completely re-encode the video stream from the HDMI encoder. This process fixes potential stream corruption and compatibility issues, ensuring a smooth playback experience in Channels. However, please be aware that this real-time re-encoding is CPU-intensive.

## **How It Works**

1. **Channels DVR** requests a channel from the M3U playlist provided by this server.  
2. The **Bridge Server** receives the request and finds an available Roku/encoder pair from its pool.  
3. It sends an **ECP deep link command** to the Roku, telling it to tune to the correct app and channel (e.g., YouTube TV, Philo).  
4. The server then connects to the corresponding **HDMI encoder**, captures the video stream, and uses ffmpeg to re-encode it in real-time.  
5. The clean, re-encoded stream is sent back to Channels DVR for viewing or recording.

## **Setup and Installation**

### **Step 1: Download the Project Files**

1. On the main page of the GitHub repository, click the green **\<** \> Code button.  
2. In the dropdown menu, click **Download ZIP**.  
3. Extract the ZIP file to a folder on the computer where you run Docker.

### **Step 2: Configure roku\_channels.json**

This is the most important step. You must edit the roku\_channels.json file to match your specific hardware and channel lineup.

The file is split into two sections: tuners and channels.

#### **tuners**

This is a list of your physical hardware setups. Each object in the list represents one Roku and its dedicated HDMI encoder.

* name: A friendly name for the device pair (e.g., "Living Room TV").  
* roku\_ip: The IP address of the Roku device.  
* encoder\_url: The full URL of the video stream from the corresponding HDMI encoder.  
* priority: A number that determines the order in which tuners are used. A lower number means higher priority. The script will always try to use the lowest priority available tuner first.

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

This is a list of all the channels you want to make available in Channels DVR.

* id: A unique, simple identifier for the channel (no spaces).  
* name: The display name of the channel.  
* roku\_app\_id: The application ID for the Roku app (e.g., "20197" for YouTube TV, "196460" for Philo).  
* deep\_link\_content\_id: The specific content ID required to deep link directly to the channel within the app.  
* media\_type: Usually "live" for streaming channels.  
* tvc\_guide\_stationid: The station ID that Channels DVR uses to fetch the correct guide data.

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

Open a terminal or PowerShell, navigate to the folder containing the project files, and run the following command. This will build the Docker image and automatically copy your roku\_channels.json into it.

```

docker build -t roku-channels-bridge .

```

### **Step 4: Run the Docker Container**

Run the container using the command below. This command maps a port from your host machine to the container and ensures it restarts automatically.

**For Windows (PowerShell/CMD):**

```

docker run -d --name roku-channels-bridge -p 5006:5000 -v C:\path\to\your\config-folder:/app/config --restart unless-stopped roku-channels-bridge

```

**For Linux / macOS:**

```

docker run -d --name roku-channels-bridge -p 5006:5000 -v /path/to/your/config-folder:/app/config --restart unless-stopped roku-channels-bridge

```

**Note:** Replace C:\\path\\to\\your\\config-folder or /path/to/your/config-folder with the actual path to the folder on your host machine where your roku\_channels.json file is located. This allows you to update the config without rebuilding the image.

## **Usage in Channels DVR**

1. Open your Channels DVR server settings.  
2. Under "Sources," click "Add Source" and choose "Custom Channels."  
3. Set the following values:  
   * **Source:** M3U Playlist  
   * **URL:** http://\<IP\_OF\_DOCKER\_HOST\>:5006/channels.m3u  
4. Save the source. Channels will automatically import your channels and download the guide data.

## **Future Development**

The primary focus for the next release will be to \*\*
