-----

# Multi-View Channels DVR Streamer

This Flask application combines multiple Channels DVR live streams into a single multi-view H.264/MPEG-TS output, with dynamic audio switching and video highlight capabilities via a simple API.

## Requirements

  * **Docker:** Used to containerize the application and its FFmpeg dependency.
  * **Channels DVR Server:** An active Channels DVR server accessible from where this Docker container is running.
  * **FFmpeg with `libzmq` support:** The Dockerfile provided will install FFmpeg from Debian's repositories, which includes `libzmq`.

## Setup and Run

1.  **Download and Extract the Project:**

      * Click the green "Code" button.
      * Select "Download ZIP".
      * Extract the contents of the ZIP file to a folder on your computer.

2.  **Navigate to the Project Directory:**
    Open your terminal or command prompt and change your directory to the extracted project folder:

    ```bash
    cd /path/to/your/extracted/multiview-project
    ```

    (Replace `/path/to/your/extracted/multiview-project` with the actual path).

3.  **Build the Docker Image:**
    This command builds the Docker image.

    ```bash
    docker build -t multiviewer .
    ```

4.  **Run the Docker Container:**
    Replace `192.168.86.64` with the actual IP address of your Channels DVR server. The `5006:5001` port mapping means the Flask app inside the container listens on `5001`, but it's accessible on your host machine's port `5006`.

    ```bash
    docker run -p 5006:5001 \
        -e CDVR_HOST="192.168.86.64" \
        -e CDVR_PORT="8089" \
        -e CODEC="libx264" \
        multiviewer
    ```

      * `-p 5006:5001`: Maps host port `5006` to container port `5001`.
      * `-e CDVR_HOST`: IP address of your Channels DVR server.
      * `-e CDVR_PORT`: Port of your Channels DVR server (default is 8089).
      * `-e CODEC`: Video codec (`libx264` for software encoding, `h264_qsv` for Intel Quick Sync Video if configured).

## Usage

### 1\. View the Combined Stream

Once the Docker container is running, open a video player like **VLC** (or a browser that supports MPEG-TS streams) and open the network stream:

```
http://localhost:5006/combine?ch=6014&ch=6018&ch=6043&ch=6044
```

Replace `6014`, `6018`, `6043`, `6044` with the channel numbers you wish to combine (up to 4 channels).

### 2\. Switch Audio and Highlight (Dynamic Control)

You can send POST requests to change the active audio track and highlight the corresponding video stream in real-time.

Use `curl` from a **separate terminal** on your host machine:

  * **Switch to the 1st stream's audio/video (index 0):**
    ```bash
    curl -X POST -H "Content-Type: application/json" -d '{"track_index": 0}' http://localhost:5006/switch_stream
    ```
  * **Switch to the 2nd stream's audio/video (index 1):**
    ```bash
    curl -X POST -H "Content-Type: application/json" -d '{"track_index": 1}' http://localhost:5006/switch_stream
    ```
  * **Switch to the 3rd stream's audio/video (index 2):**
    ```bash
    curl -X POST -H "Content-Type: application/json" -d '{"track_index": 2}' http://localhost:5006/switch_stream
    ```
  * **Switch to the 4th stream's audio/video (index 3):**
    ```bash
    curl -X POST -H "Content-Type: application/json" -d '{"track_index": 3}' http://localhost:5006/switch_stream
    ```

-----
