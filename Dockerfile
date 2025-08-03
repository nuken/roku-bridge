# Use an official Python runtime as a parent image
FROM python:3.9-slim-buster

# Set the working directory in the container
WORKDIR /app

# Update apt sources to archive for older Debian versions FIRST.
# Then install curl, ffmpeg, and the necessary Intel drivers for QSV.
# We also add the "non-free" repository which is required for the Intel driver.
RUN sed -i 's/deb.debian.org/archive.debian.org/g' /etc/apt/sources.list \
    && sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list \
    && echo "deb http://archive.debian.org/debian/ buster main contrib non-free" >> /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       ffmpeg \
       intel-media-va-driver-non-free \
       vainfo \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# Make sure gevent is in your requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Create a directory for persistent configuration
RUN mkdir -p /app/config

# --- NEW ---
# Copy the local roku_channels.json into the container's config directory.
# This will be used on first run. It can be overwritten later by uploading.
COPY roku_channels.json /app/config/roku_channels.json

# Copy the application code into the container
COPY app.py .

# Expose the port the app runs on
EXPOSE 5000

# Run the application using the gevent async worker
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--timeout", "0", "app:app"]
