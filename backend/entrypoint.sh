#!/bin/bash
set -e

ROLE="${APP_ROLE:-backend}"

if [ "${ROLE}" = "worker" ]; then
    echo "Starting clustering worker..."
    exec python -u -c "import app; app.run_clustering_worker()"
fi

echo "Starting Flask backend with gunicorn..."
# Use GUNICORN_WORKERS env var if set, otherwise default to 1
WORKERS="${GUNICORN_WORKERS:-1}"
# Threaded workers: the app is I/O-bound (table/blob storage round-trips), and a
# sync worker serves exactly one request at a time — every other call queues
# behind it, so one slow request (a big metadata scan, a video finalize) made
# the whole API unresponsive and let the request backlog turn into 503s.
THREADS="${GUNICORN_THREADS:-8}"
exec gunicorn app:app \
    --bind 0.0.0.0:5000 \
    --workers "$WORKERS" \
    --worker-class gthread \
    --threads "$THREADS" \
    --timeout 600 \
    --graceful-timeout 120 \
    --keep-alive 30
