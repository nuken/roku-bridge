# Use a more modern and supported Python runtime as a parent image
FROM python:3.9-slim-bullseye

# Set the working directory in the container
WORKDIR /app

# Add the "non-free" component, update, and install dependencies.
# The Intel driver is installed conditionally only on the amd64 architecture.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i 's/main/main contrib non-free/g' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       curl \
       ffmpeg \
       vainfo \
    && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
         apt-get install -y --no-install-recommends intel-media-va-driver-non-free; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python requirements
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a mount point for persistent configuration
RUN mkdir -p /app/config

# Copy the application code, plugins, and templates
COPY app.py .
COPY plugins/ /app/plugins/
COPY templates/ /app/templates/
COPY static/ /app/static/

# Expose the port the app runs on
EXPOSE 5000

# Run the application with a single worker to ensure config reloads work correctly
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "1", "--timeout", "0", "app:app"]