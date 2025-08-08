# Use a more modern and supported Python runtime as a parent image
FROM python:3.9-slim-bullseye

# Set the working directory in the container
WORKDIR /app

# Add the "non-free" component to a new sources list file, then install dependencies
# This is a more robust method than modifying the existing sources.list
RUN echo "deb http://deb.debian.org/debian bullseye main contrib non-free" > /etc/apt/sources.list.d/non-free.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       ffmpeg \
       intel-media-va-driver-non-free \
       vainfo \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create a mount point for persistent configuration
RUN mkdir -p /app/config

# Copy the application code and templates
COPY app.py .
COPY templates/ /app/templates/

# Expose the port the app runs on
EXPOSE 5000

# Run the application
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--timeout", "0", "app:app"]
