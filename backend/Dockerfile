# Dockerfile for Flask backend (Azure Container Apps)
FROM mirror.gcr.io/library/python:3.11-slim

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
COPY requirements-clustering.txt ./
RUN pip install -r requirements.txt -r requirements-clustering.txt

# Copy runtime Python modules used by app.py
COPY *.py ./
COPY routes/ ./routes/
COPY data/ ./data/
COPY entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

# Expose port (Flask default is 5000). 5001 is the gunicorn-master-owned
# health sidecar (see gunicorn.conf.py's on_starting) -- stays up independent
# of whether app worker threads are saturated.
EXPOSE 5000
EXPOSE 5001

# Set environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV GUNICORN_WORKERS=1
ENV GUNICORN_THREADS=2

# Start the role-aware entrypoint.
CMD ["/app/entrypoint.sh"]
