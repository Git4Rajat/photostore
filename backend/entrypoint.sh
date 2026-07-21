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
exec gunicorn app:app \
    --bind 0.0.0.0:5000 \
    --workers "$WORKERS" \
    --worker-class sync \
    --timeout 600 \
    --graceful-timeout 120 \
    --keep-alive 30
