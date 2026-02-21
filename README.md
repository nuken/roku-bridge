# **Roku Channels Bridge (LEAN Edition)**

**Release: 5.0.0-LEAN**

**[Official Configuration Guide](https://tuner.ct.ws)**

This "Lean and Mean" edition of the Roku Channels Bridge is a high-performance, ultra-lightweight proxy designed specifically for use with **LinkPi encoders**. By stripping out legacy transcoding features (`ffmpeg`) and complex macro systems, this version focuses entirely on lightning-fast tuning using Roku's native ECP deep-linking.

It is highly optimized for stable, deep-link-friendly streaming apps like **YouTube TV** and **DirecTV Stream**.

## **Key Features**

* **Instant Deep-Linking:** Bypasses the Roku search menu and pushes the content ID directly into the app's internal player for the fastest possible tuning.
* **Zero-Overhead Proxy:** Built strictly for hardware encoders like LinkPi that do not require re-encoding. It acts as a pure, high-speed pass-through for the video stream.
* **Integrated Gracenote Auto-Mapping:** You no longer need a separate EPG/XMLTV file. Simply enter the Gracenote Station ID in the web interface, and Channels DVR will automatically map the guide data and channel logos.
* **M3U Playlist Filtering:** Group channels together in the web UI and instantly generate separate, filtered M3U URLs for different DVR sources.
* **Ultra-Lightweight Image:** Removed all `ffmpeg` and hardware-acceleration dependencies, resulting in a significantly smaller Docker footprint and near-zero CPU usage.

## **Installation**

The application is distributed as a multi-architecture Docker image.

### **Step 1: Pull the Docker Image**

Open a terminal and pull the `lean` tagged image from Docker Hub:

```bash
docker pull rcvaughn2/roku-ecp-tuner:lean

```

### **Step 2: Run the Docker Container**

Run the container using the command below. This creates a persistent Docker volume named `roku-bridge-config` where your configuration will be safely stored.

```bash
docker run -d \
  --name roku-channels-bridge-lean \
  -p 5006:5000 \
  -v roku-bridge-config:/app/config \
  --restart unless-stopped \
  rcvaughn2/roku-ecp-tuner:lean

```

### **Step 3: Configure Your Tuners & Channels**

1. Open your web browser and navigate to the Status & Configuration Page:
`http://<IP_OF_DOCKER_HOST>:5006/status`
2. Use the web interface to:
* **Add Your LinkPi Tuners:** Click "Add Tuner" and provide the Roku IP and LinkPi TS stream URL.
* **Add Deep-Link Channels:** Click "Add Channel". Select your target app (YouTube TV or DirecTV) from the dropdown, enter the Deep Link Content ID, and add the Gracenote Station ID.
* **Assign Playlists:** (Optional) Enter a group name like "Sports" or "YTTV" to group your channels.
* **Set Tune Delays:** Adjust the delay (in seconds) to give the app enough time to load the video before the bridge starts proxying the stream.


3. Click **Save & Reload Server** to apply your changes instantly.

## **Channels DVR Setup**

This lean bridge generates an M3U playlist file that handles both the stream routing and the guide data mapping.

* **Master M3U URL:** `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u`

**To add to Channels DVR:**

1. Open your Channels DVR server settings.
2. Go to "Sources" and click "+ Add Source" -> "Custom Channels".
3. Enter the M3U URL.
4. Because the M3U includes the `tvc-guide-stationid` tags you configured in the web UI, Channels DVR will automatically download the correct guide data.

### **Playlist Filtering (Creating Multiple Sources)**

If you used the **Playlist Group** field in the web UI to organize your channels, you can import them into Channels DVR as completely separate sources.

To filter the M3U output, just add `?playlist=<YourGroupName>` to the end of the URL.

* *Example 1 (Only YouTube TV Channels):* `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u?playlist=YTTV`
* *Example 2 (Only Sports Channels):* `http://<IP_OF_DOCKER_HOST>:5006/channels.m3u?playlist=Sports`

## **Configuration File (`roku_channels.json`)**

While it is highly recommended to manage your setup through the web interface, the raw configuration is stored in `roku_channels.json`. Here is an example of the streamlined structure:

```json
{
  "tuners": [
    {
      "name": "LinkPi-1",
      "roku_ip": "192.168.86.35",
      "encoder_url": "http://192.168.86.90/ts/1_0"
    }
  ],
  "channels": [
    {
      "id": "yttv_fox",
      "name": "FOX",
      "roku_app_id": "195316",
      "deep_link_content_id": "Gs-ILaF-HNw",
      "gracenote_id": "11594",
      "playlist": "YTTV",
      "tune_delay": 3
    }
  ]
}

```
