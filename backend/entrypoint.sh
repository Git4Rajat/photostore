#!/bin/bash
set -e

ROLE="${APP_ROLE:-backend}"

if [ "${ROLE}" = "worker" ]; then
    echo "Starting clustering worker..."
    exec python -u -c "import app; app.run_clustering_worker()"
fi

if [ "${ROLE}" = "ipworker" ]; then
    echo "Starting ipworker..."
    # tesserocr (ipwork_ocr.py) needs TESSDATA_PREFIX explicitly -- unlike
    # the tesseract CLI binary pytesseract used to shell out to, the
    # in-process libtesseract tesserocr links against does not reliably
    # auto-discover the apt-installed trained-data directory (confirmed
    # failing locally with "Failed to init API, possibly an invalid
    # tessdata path" when unset). Discover it once at container start
    # rather than hardcoding a path that could shift with the
    # tesseract-ocr package version.
    TESSDATA_DIR="$(dirname "$(find /usr/share -name 'eng.traineddata' 2>/dev/null | head -n1)" 2>/dev/null || true)"
    if [ -n "$TESSDATA_DIR" ] && [ "$TESSDATA_DIR" != "." ]; then
        export TESSDATA_PREFIX="$TESSDATA_DIR"
    fi
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
# With --workers 1, a max-requests recycle stops the sole worker from
# accepting new connections until it either drains its in-flight request(s)
# or graceful-timeout forces it out -- there is no second worker to cover the
# gap, so this window is a full capacity-zero blackout (observed live: up to
# ~120s, producing bursts of 503s). Bounded to 60s -- comfortably above the
# ~33-37s average for the slowest known endpoint (/upload/client-processing)
# so that request isn't routinely killed mid-flight, while capping the
# outage far below the old 120s ceiling.
exec gunicorn app:app \
    --config gunicorn.conf.py \
    --bind 0.0.0.0:5000 \
    --workers "$WORKERS" \
    --worker-class gthread \
    --threads "$THREADS" \
    --max-requests "$MAX_REQUESTS" \
    --max-requests-jitter "$MAX_REQUESTS_JITTER" \
    --timeout 600 \
    --graceful-timeout 60 \
    --keep-alive 30
