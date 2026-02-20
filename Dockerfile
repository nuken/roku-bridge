FROM python:3.13-slim-bullseye
WORKDIR /app

# Only install curl for health checks, remove ffmpeg dependencies
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only copy core files
COPY app.py .
COPY templates/ /app/templates/
COPY static/ /app/static/

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--worker-class", "gevent", "--workers", "1", "app:app"]
