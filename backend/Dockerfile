# syntax=docker/dockerfile:1.7
# Dockerfile for Flask backend (Azure Container Apps)
FROM python:3.11-slim

# Keep container build logs clean when pip runs as root in Docker layers.
ENV PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ffmpeg \
    libffi-dev \
    libimage-exiftool-perl \
    libraw-bin \
    libssl-dev \
    libgomp1 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set workdir
WORKDIR /app

# Copy requirements and install
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy runtime Python modules used by app.py
COPY *.py ./
COPY entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

# Expose port (Flask default is 5000)
EXPOSE 5000

# Set environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV GUNICORN_WORKERS=1

# Start the role-aware entrypoint.
CMD ["/app/entrypoint.sh"]
