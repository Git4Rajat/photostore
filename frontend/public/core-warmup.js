(function () {
    var base = window.__APP_CONFIG__ && window.__APP_CONFIG__.apiBaseUrl;
    if (!base || window.__keepsakeCoreWarmupStarted || typeof fetch !== 'function') return;
    window.__keepsakeCoreWarmupStarted = true;

    // Independent of the application bundle, authentication and gallery readiness.
    var url = base.replace(/\/$/, '') + '/health';
    async function wake() {
        for (var attempt = 0; attempt < 6; attempt += 1) {
            if (navigator.onLine === false) return;
            var controller = new AbortController();
            var timer = setTimeout(function () { controller.abort(); }, 8000);
            try {
                var response = await fetch(url, {
                    signal: controller.signal, credentials: 'omit', cache: 'no-store',
                });
                if (response.body) await response.body.cancel();
                if (response.status < 500) return;
            } catch (_) {
                // Cold ingress failures must not delay or reject the application boot.
            } finally {
                clearTimeout(timer);
            }
            if (attempt < 5) {
                await new Promise(function (resolve) {
                    setTimeout(resolve, Math.min(1000 * Math.pow(2, attempt), 4000));
                });
            }
        }
    }
    void wake();
}());
