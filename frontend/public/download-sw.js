// Service worker for the client-orchestrated library export
// (see src/services/serviceWorkerDownload.ts and libraryExportDownloader.ts).
//
// Exists to solve one specific problem: handing a large in-browser-fetched
// file to the user via a plain `<a download href="blob:...">` click hits two
// real, separate limits --
//   1. Safari has a long-standing bug ("WebKitBlobResource error") failing
//      large blob: URL downloads outright -- confirmed live 2026-09-05/06,
//      not a theoretical risk.
//   2. A direct link to the ORIGINAL cross-origin blob storage URL avoids
//      that bug, but browsers only honor a chosen filename (the `download`
//      attribute, or a server's Content-Disposition) for same-origin/blob:/
//      data: URLs -- a cross-origin link to an anonymized (UUID-named) blob
//      would save as the UUID, not the real photo name.
//
// This worker intercepts fetches for a fake same-origin path and answers
// them itself with bytes the page already has (already fetched+resumed via
// OPFS, see opfsDownloadStaging.ts) -- to the browser that's an ordinary
// same-origin HTTP download, so neither limit applies.

const DOWNLOAD_PREFIX = '/__library-export-download__/';

// id -> { filename, buffer, timeoutId }. Entries are removed as soon as
// they're served; the timeout is only a safety net for a registered file
// whose download link never actually gets requested (e.g. the page
// navigated away between registering and clicking).
const pending = new Map();
const PENDING_TIMEOUT_MS = 120000;

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    // Without this, a freshly (re)installed worker doesn't control pages
    // that were already open -- every download would need a full reload
    // first, which isn't a reasonable thing to ask mid-export.
    event.waitUntil(self.clients.claim());
});

self.addEventListener('message', (event) => {
    const data = event.data;
    if (!data || data.type !== 'register-download') return;
    const { id, filename, buffer } = data;
    const timeoutId = setTimeout(() => { pending.delete(id); }, PENDING_TIMEOUT_MS);
    pending.set(id, { filename: String(filename || 'photo'), buffer, timeoutId });
    const port = event.ports && event.ports[0];
    if (port) port.postMessage({ ok: true });
});

const contentDispositionFor = (filename) => {
    // Mirrors backend/app.py's _download_content_disposition: a plain-ASCII
    // fallback plus an RFC 5987 filename* so non-ASCII names survive too.
    const safe = filename.replace(/["\r\n\\]/g, '_');
    const asciiFallback = safe.replace(/[^\x20-\x7E]/g, '_') || 'photo';
    return `attachment; filename="${asciiFallback}"; filename*=UTF-8''${encodeURIComponent(safe)}`;
};

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (!url.pathname.startsWith(DOWNLOAD_PREFIX)) return; // not ours -- let it through untouched

    const id = url.pathname.slice(DOWNLOAD_PREFIX.length).split('/')[0];
    const entry = pending.get(id);
    if (!entry) {
        event.respondWith(new Response('Not found', { status: 404 }));
        return;
    }
    clearTimeout(entry.timeoutId);
    pending.delete(id);
    event.respondWith(new Response(entry.buffer, {
        status: 200,
        headers: {
            'Content-Type': 'application/octet-stream',
            'Content-Length': String(entry.buffer.byteLength),
            'Content-Disposition': contentDispositionFor(entry.filename),
            'Cache-Control': 'no-store',
        },
    }));
});
