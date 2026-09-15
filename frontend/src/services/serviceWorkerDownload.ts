// Page-side half of public/download-sw.js -- see that file for why this
// exists (Safari's blob: URL download bug, and cross-origin links dropping a
// chosen filename). Registers the worker, hands it each file's already-
// fetched bytes over a MessageChannel, then triggers a same-origin download
// link the worker answers itself.

const DOWNLOAD_PREFIX = '/__library-export-download__/';

export const isServiceWorkerDownloadSupported = (): boolean => (
    typeof navigator !== 'undefined' && 'serviceWorker' in navigator
);

let registrationPromise: Promise<ServiceWorkerRegistration> | null = null;

// A forced/hard reload (shift+reload, or "Empty Cache and Hard Reload" in
// DevTools) deliberately leaves that page load uncontrolled by any service
// worker for its *entire* lifetime -- confirmed live 2026-09-06 with a CDP
// Page.reload({ ignoreCache: true }): navigator.serviceWorker.controller
// stays null and 'controllerchange' never fires, even though the worker is
// registered and activated. Without a timeout here, awaiting that event
// hangs forever with zero error and zero progress -- exactly the "download
// entire library does nothing after a hard refresh" report. A plain
// (non-hard) reload reliably re-attaches control, and the export's own
// localStorage auto-resume flag + IndexedDB per-file status pick up right
// where this load left off, so reloading once is a safe recovery, not a
// disruption.
const CONTROLLER_WAIT_TIMEOUT_MS = 4000;
const RELOAD_ATTEMPTED_KEY = 'photostore.swControlReloadAttempted';

// Registers the worker (idempotent -- the browser no-ops a re-register of an
// unchanged script) and waits until it's actually controlling this page.
// clients.claim() in the worker's activate handler means a freshly
// registered worker takes control without needing a reload, but there's
// still a brief window right after registration where
// navigator.serviceWorker.controller is still null -- wait for
// 'controllerchange' rather than assume it's immediate.
const ensureServiceWorkerActive = async (): Promise<void> => {
    if (!isServiceWorkerDownloadSupported()) {
        throw new Error('Service workers are not supported in this browser.');
    }
    if (!registrationPromise) {
        registrationPromise = navigator.serviceWorker.register('/download-sw.js');
    }
    await registrationPromise;
    await navigator.serviceWorker.ready;
    if (navigator.serviceWorker.controller) {
        sessionStorage.removeItem(RELOAD_ATTEMPTED_KEY);
        return;
    }
    const gotControl = await new Promise<boolean>((resolve) => {
        const onChange = () => {
            navigator.serviceWorker.removeEventListener('controllerchange', onChange);
            resolve(true);
        };
        navigator.serviceWorker.addEventListener('controllerchange', onChange);
        setTimeout(() => {
            navigator.serviceWorker.removeEventListener('controllerchange', onChange);
            resolve(false);
        }, CONTROLLER_WAIT_TIMEOUT_MS);
    });
    if (gotControl) {
        sessionStorage.removeItem(RELOAD_ATTEMPTED_KEY);
        return;
    }
    // Only reload once per tab session -- a browser that categorically never
    // grants control (service workers disabled outright, some Safari Private
    // Browsing configurations) should fail loudly on the second attempt
    // instead of reload-looping forever.
    if (!sessionStorage.getItem(RELOAD_ATTEMPTED_KEY)) {
        sessionStorage.setItem(RELOAD_ATTEMPTED_KEY, '1');
        window.location.reload();
        // Navigation is already underway; never resolve so the caller doesn't
        // start fetch/stage work against a page that's about to be torn down.
        await new Promise<void>(() => {});
    }
    throw new Error('This browser is not letting the page manage downloads. Try a normal refresh (not a hard/force refresh) and try again.');
};

// Hands the worker one file's bytes and waits for it to confirm storage
// before the caller triggers the download link -- without this ack, the
// download request could reach the worker before the message did.
//
// Deliberately reads the whole file into an ArrayBuffer first rather than
// passing the OPFS-backed File/Blob across postMessage directly -- tried
// that (it would have avoided materializing large videos fully in memory)
// and confirmed live 2026-09-06 it corrupts the download: this codebase
// deletes the OPFS staging entry (removeStagedFile) right after triggering
// the save, since there's no reliable "browser actually finished the
// download" signal available to page JS to wait for first (see
// triggerPreparedDownload below). An OPFS-backed File's snapshot does NOT
// survive deletion of its underlying entry once structured-cloned into the
// service worker's context in Chrome -- unlike a plain in-memory Blob, doing
// that reproducibly turned the download into Playwright's "canceled"
// failure. Reading into an ArrayBuffer up front fully decouples the
// transferred bytes from OPFS's lifecycle, at the cost of holding one file's
// bytes in memory per in-flight save (bounded by EXPORT_CONCURRENCY).
const registerWithServiceWorker = (id: string, filename: string, buffer: ArrayBuffer): Promise<void> => new Promise((resolve, reject) => {
    const controller = navigator.serviceWorker.controller;
    if (!controller) {
        reject(new Error('Download helper is not active.'));
        return;
    }
    const channel = new MessageChannel();
    channel.port1.onmessage = (event) => {
        if (event.data && event.data.ok) resolve();
        else reject(new Error('Download helper failed to register the file.'));
    };
    controller.postMessage({ type: 'register-download', id, filename, buffer }, [buffer, channel.port2]);
});

// Phase 1: hands `file`'s bytes to the service worker and returns the
// same-origin URL that will serve them back. Safe to run concurrently across
// several files at once -- nothing here is visible to the browser as a
// navigation yet.
export const prepareServiceWorkerDownload = async (filename: string, file: File): Promise<string> => {
    await ensureServiceWorkerActive();
    const buffer = await file.arrayBuffer();
    const id = crypto.randomUUID();
    await registerWithServiceWorker(id, filename, buffer);
    return `${DOWNLOAD_PREFIX}${id}/${encodeURIComponent(filename)}`;
};

// Phase 2: the actual save trigger for a URL prepareServiceWorkerDownload
// already returned. Deliberately no anchor.download attribute: confirmed
// live 2026-09-06 that Chrome routes a download-attributed navigation
// through an internal path that *bypasses the service worker's fetch event
// entirely* -- a plain fetch() to the exact same URL was intercepted
// correctly, but the <a download> click wasn't, and silently fell through to
// whatever the network actually had at that path. The worker's own
// Content-Disposition: attachment header is what triggers the save instead
// (and has been sufficient to do that in every browser since long before the
// download attribute existed) -- a plain navigation-type request does go
// through the fetch event, and the browser recognizes the attachment
// disposition before it ever considers navigating the tab away.
//
// Callers MUST serialize calls to this function (never call it again before
// the previous call has fully returned) -- confirmed live 2026-09-06 that
// two of these firing close together are two overlapping main-frame
// navigations in the same tab, and the browser silently cancels one in favor
// of the other with no signal to page JS. libraryExportDownloader.ts runs
// this behind a strict one-at-a-time queue, not just a pacing delay between
// *starting* each file's save -- the delay alone wasn't enough, since the
// prepare phase's own await time could still let two triggers land together.
export const triggerPreparedDownload = (url: string): void => {
    const anchor = document.createElement('a');
    anchor.href = url;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
};
