#!/bin/bash
set -e

ROLE="${APP_ROLE:-backend}"

if [ "${ROLE}" = "worker" ]; then
    echo "Starting clustering worker..."
    exec python -u -c "import app; app.run_clustering_worker()"
fi

if [ "${ROLE}" = "ipworker" ]; then
    echo "Starting ipworker..."
    exec python -u -c "import app; app.run_ipworker()"
fi

echo "Starting Flask backend with gunicorn..."
# Use GUNICORN_WORKERS env var if set, otherwise default to 1
WORKERS="${GUNICORN_WORKERS:-1}"
# Threaded workers: the app is I/O-bound (table/blob storage round-trips), and a
# sync worker serves exactly one request at a time — every other call queues
# behind it, so one slow request (a big metadata scan, a video finalize) made
# the whole API unresponsive and let the request backlog turn into 503s.
THREADS="${GUNICORN_THREADS:-2}"
# Recycle each worker after a bounded number of requests (+/- jitter so the
# workers don't all recycle in lockstep). Image/RAW/video processing fragments
# the heap and leaves numpy/Pillow allocations that the allocator is slow to
# return to the OS, so a long-lived worker's RSS creeps upward until an OOM
# kill takes the whole replica down. Periodic recycling returns that memory and
# keeps steady-state RSS well under the container limit.
MAX_REQUESTS="${GUNICORN_MAX_REQUESTS:-400}"
MAX_REQUESTS_JITTER="${GUNICORN_MAX_REQUESTS_JITTER:-50}"
exec gunicorn app:app \
    --bind 0.0.0.0:5000 \
    --workers "$WORKERS" \
    --worker-class gthread \
    --threads "$THREADS" \
    --max-requests "$MAX_REQUESTS" \
    --max-requests-jitter "$MAX_REQUESTS_JITTER" \
    --timeout 600 \
    --graceful-timeout 120 \
    --keep-alive 30
