import os


def post_fork(server, worker):
    # Backend workers get recycled every ~400 requests (see entrypoint.sh),
    # so this can't be a one-time startup cost -- it needs to happen after
    # every fork, or the first synchronous reverse-geocode call on each
    # fresh worker (MAPS_ON_UPLOAD=true) would pay the ~1s offline K-D tree
    # build inline on a real upload request.
    import maps_utils
    maps_utils.prewarm_offline_geocoder()


def on_starting(server):
    # Liveness sidecar: a trivial stdlib-only HTTP server on its own port,
    # started once in the gunicorn MASTER process (on_starting runs before
    # any worker is forked, and only here -- never again per-worker) so it
    # stays up for as long as the master does, completely independent of
    # whether app workers are busy.
    #
    # 2026-10-01: added after a live ContainerBackOff crash loop on
    # microsvcpoc-dev traced to GUNICORN_WORKERS=1/THREADS=2 -- a slow
    # request (access-batch's Table Storage fallback) could occupy both of
    # the only worker's request threads, leaving nothing to answer Azure's
    # liveness probe ("connection refused" in system events) even though the
    # process itself was alive and would have recovered on its own. Pointing
    # the probe at this port instead means it keeps answering even when
    # every app thread is saturated -- it only goes down if the master
    # itself is actually dead, which is the one case a restart is the right
    # call. Deliberately has zero dependency on app.py/Flask/storage clients
    # so it can never be blocked by anything the app is doing.
    #
    # HEALTH_SIDECAR_PORT='' (empty) disables this -- e.g. for a local/dev
    # run where nothing probes it and an extra bound port is just noise.
    port_raw = os.environ.get('HEALTH_SIDECAR_PORT', '5001').strip()
    if not port_raw:
        return
    try:
        port = int(port_raw)
    except ValueError:
        return

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler's naming convention
            body = b'OK'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 -- stdlib's own signature
            pass  # Silence per-request access logs; this is probed every few seconds.

    try:
        httpd = ThreadingHTTPServer(('0.0.0.0', port), _HealthHandler)
    except OSError:
        server.log.exception('Health sidecar failed to bind port %s', port)
        return
    threading.Thread(target=httpd.serve_forever, name='health-sidecar', daemon=True).start()
    server.log.info('Health sidecar listening on 0.0.0.0:%s', port)
