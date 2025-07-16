#docker buildx build --platform linux/amd64 -f Dockerfile -t bnhf/multichannelview:test . --push --no-cache
FROM debian:bookworm-slim

# Set environment variables for non-interactive installation
ENV DEBIAN_FRONTEND=noninteractive

# Update package lists and install necessary dependencies
# This includes build tools, FFmpeg and its development libraries, and libzmq.
# libzmq3-dev is crucial for FFmpeg's azmq filter.
# python3 and python3-pip are for the Flask application.
# gunicorn is for a more robust production server.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    curl \
    nano \
    ffmpeg \
    libzmq3-dev \
    gunicorn && \
    # Attempt to install Intel VA-API drivers for QSV, allow failure if not found
    apt-get install -y --no-install-recommends intel-media-va-driver-non-free vainfo || \
    echo "Warning: intel-media-va-driver-non-free or vainfo not found, continuing without them. QSV might not work." && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Install Python dependencies, including pyzmq for ZeroMQ communication
# --break-system-packages is needed on Debian Bookworm for global pip installs
RUN pip3 install --break-system-packages flask pyzmq

# Copy the Flask application script into the container
COPY flask_app.py .

# Expose the port your Flask app will run on
EXPOSE 5001

# Define environment variables for the Flask app (optional, but good practice)
# These can be overridden when running the container using -e
ENV CDVR_HOST="192.168.86.64"
ENV CDVR_PORT="8089"
# Default to software encoding. Change to 'h264_qsv' if QSV is properly configured on host and container.
ENV CODEC="libx264"

# Command to run the Flask application using Gunicorn (recommended for production)
# Use 0.0.0.0 to make it accessible from outside the container
# -w 1 means 1 worker process. For simple use cases, this is fine.
# For multi-client scenarios, you'd need a more advanced ZeroMQ setup or a single-worker model.
# Increased timeout to 60 seconds (default is usually 30)
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5001", "--timeout", "120", "flask_app:app"]
