"""Blueprint: system routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

system_bp = Blueprint('system', __name__)

@system_bp.route('/health', methods=['GET'])
def health_check():
    return app.jsonify({
        'status': 'healthy',
        'service': 'photo-store-api',
        'storage_account': app.account_name,
        'uses_managed_identity': app.credential is not None,
    })

@system_bp.route('/geocode/reverse', methods=['GET'])
@system_bp.route('/api/geocode/reverse', methods=['GET'])
def geocode_reverse():
    """Server-side reverse geocode, used by the browser AI pipeline's
    map_detection step. Proxying through the backend (instead of the browser
    calling a third-party geocoder directly) avoids depending on that
    service's CORS/availability from an arbitrary browser origin -- the same
    maps_utils.reverse_geocode() call already runs cheaply (1-116ms) from the
    ipworker path."""
    _user_id, error = app._require_user_id()
    if error:
        return error
    latitude = (app.request.args.get('lat') or '').strip()
    longitude = (app.request.args.get('lon') or '').strip()
    if not latitude or not longitude:
        return app.jsonify({'error': 'lat and lon are required'}), 400
    import maps_utils
    try:
        result = maps_utils.reverse_geocode(latitude, longitude)
    except Exception:
        app.worker_logger.exception('geocode_reverse failed')
        result = {}
    return app.jsonify(result or {})

@system_bp.route('/performance/throughput', methods=['GET'])
@system_bp.route('/performance/throughput/', methods=['GET'])
@system_bp.route('/api/performance/throughput', methods=['GET'])
@system_bp.route('/api/performance/throughput/', methods=['GET'])
def performance_throughput():
    return app.jsonify(app._get_throughput_metrics())

@system_bp.route('/api/jobs/status', methods=['GET'])
@system_bp.route('/jobs/status', methods=['GET'])
def jobs_status():
    """Return the current user's recent background jobs so the in-app notifier
    can surface completions (reclustering, find-more-faces, library cleanup,
    preview generation, ...). Includes anything still queued/running plus jobs
    that finished within the recent window; the client dedupes what it has
    already shown so a completed job is only ever announced once.
    """
    try:
        user_id, error = app._require_user_id()
        if error:
            return error
        if app.metadata_table_client is None:
            return app.jsonify({'jobs': []})
        cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(minutes=app.JOB_STATUS_WINDOW_MINUTES)).isoformat()
        # A job of ANY type (clustering, ipwork, library_clean, preview, ...)
        # this old and still queued/running is dead, not in-flight — the
        # worker/ipworker crashed mid-job (e.g. OOM) and never wrote a
        # terminal status. This used to only cover job_type == 'clustering'
        # (mirroring _has_active_clustering_job's de-dupe cutoff), but any job
        # type can be orphaned the same way — an old stuck 'ipwork' row was
        # found stuck 15 days "running", keeping the server-processing
        # indicator on forever with nothing left to actually process. Without
        # this cutoff a dead row shows as perpetually "in flight" and its
        # activity indicator never clears.
        stale_cutoff = (app.datetime.now(app.timezone.utc) - app.timedelta(minutes=app.CLUSTERING_ACTIVE_JOB_STALE_MINUTES)).isoformat()
        try:
            # Same 'jobs' partition _has_active_clustering_job scans (219k+
            # rows and growing) -- userId isn't a key property, so Table
            # Storage has no secondary index for it and "...and userId eq X"
            # still costs a full partition scan server-side, paid on every
            # poll of this endpoint. Reuse the same constant-key cache
            # instead of re-scanning: one fetch of the whole partition genuinely
            # serves every user's poll within the TTL window.
            all_rows = app._jobs_partition_scan_cache.get(
                app._JOBS_PARTITION_SCAN_CACHE_KEY,
                lambda: list(app.metadata_table_client.query_entities("PartitionKey eq 'jobs'")),
            )
            rows = [row for row in all_rows if str(row.get('userId') or '') == user_id]
        except Exception:
            app.app.logger.exception('Failed to query job status rows for %s', user_id)
            return app.jsonify({'jobs': []})
        jobs = []
        flushed_any_stale = False
        for row in rows:
            status = str(row.get('status') or '').lower()
            updated_at = str(row.get('updatedAt') or '')
            job_type = str(row.get('jobType') or '')
            if status in {'queued', 'running'} and updated_at and updated_at < stale_cutoff:
                app._upsert_job_status(str(row.get('jobId') or ''), user_id, job_type, 'failed', error='Job did not finish (worker restarted or timed out)')
                flushed_any_stale = True
                continue
            # Keep in-flight jobs, plus terminal ones that finished recently.
            # updatedAt is a UTC isoformat string, so lexicographic comparison
            # against the cutoff is a valid recency test.
            if status in {'queued', 'running'} or updated_at >= cutoff:
                jobs.append(app._humanize_job(row))
        if flushed_any_stale:
            # The write(s) above just happened in this same request/process --
            # don't make the caller (or the next poller, in-process) wait out
            # the full cache TTL to see its own just-written result.
            app._jobs_partition_scan_cache.invalidate(app._JOBS_PARTITION_SCAN_CACHE_KEY)
        jobs.sort(key=lambda job: job.get('updatedAt') or '', reverse=True)
        return app.jsonify({'jobs': jobs[:50]})
    except Exception as exc:
        app.app.logger.exception('Job status route failed')
        return app.jsonify({'jobs': [], 'error': 'Internal server error'}), 500
