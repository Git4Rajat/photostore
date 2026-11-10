# Dockerfile for Flask backend (Azure Container Apps)
FROM mirror.gcr.io/library/python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

# Keep container build logs clean when pip runs as root in Docker layers.
ENV PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ffmpeg \
    libffi-dev \
    perl \
    libraw-bin \
    libssl-dev \
    libgomp1 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/install_exiftool.py /tmp/install_exiftool.py
RUN python /tmp/install_exiftool.py && rm /tmp/install_exiftool.py

# Set workdir
WORKDIR /app

# Copy requirements and install
COPY requirements.txt ./
# Hash-locked closure (see scripts/lock-requirements.sh): a tampered or
# substituted release fails the install instead of running in the image.
# The cache mount keeps pip's download cache out of the image and lets local
# rebuilds skip re-downloading wheels when requirements.txt changes.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes --no-deps -r requirements.txt

# Copy runtime Python modules used by app.py
COPY *.py ./
COPY routes/ ./routes/
COPY scripts/backfill_invitation_index.py ./scripts/backfill_invitation_index.py
COPY data/ ./data/
COPY entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

# Run as an unprivileged user: this container parses untrusted images/video
# (ffmpeg, exiftool, libraw), so a parser exploit should not land as root.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin photostore \
    && chown -R photostore:photostore /app
ENV HOME=/home/photostore
USER 10001

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
