# Stage 1: Build stage to get static FFmpeg binaries from mwader/static-ffmpeg
FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg_builder

# Stage 2: Final application image
FROM debian:bookworm-slim

# Copy the statically compiled FFmpeg and FFprobe binaries from the build stage
COPY --from=ffmpeg_builder /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg_builder /ffprobe /usr/local/bin/ffprobe

# Install Python, pip, and libzmq5 for pyzmq compatibility
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    libzmq5 \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Install Python dependencies, including gunicorn
RUN pip3 install --break-system-packages flask pyzmq gunicorn

# Copy your Flask application file into the container
COPY flask_app.py .

# Expose the port the Flask app will run on (as per README.md)
EXPOSE 5001

# Command to run the Flask application using Gunicorn with increased timeout
CMD ["gunicorn", "-b", "0.0.0.0:5001", "--timeout", "300", "flask_app:app"]
