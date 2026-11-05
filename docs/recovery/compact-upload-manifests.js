// Paste into DevTools Console on the photo app. Compatible with the current
// deployed client. Only upload parent manifests are compacted; file/blob caches,
// outcome receipts, auth settings and the active upload checkpoint are untouched.
// The previous values remain in memory until this tab is closed or reloaded.
(() => {
    const prefix = 'photostore.upload.wave-operation.v1:';
    const fields = ['key', 'name', 'size', 'type', 'lastModified', 'uploadedBytes', 'skippedDuplicate', 'status'];
    const backup = globalThis.__photostoreUploadManifestBackup ||= new Map();
    const size = () => Object.keys(localStorage).reduce((bytes, key) =>
        bytes + 2 * (key.length + (localStorage.getItem(key) || '').length), 0);
    const before = size();
    let compacted = 0;
    let skipped = 0;
    for (const storageKey of Object.keys(localStorage)) {
        if (!storageKey.startsWith(prefix)) continue;
        try {
            const raw = localStorage.getItem(storageKey);
            const parent = JSON.parse(raw);
            if (!parent || typeof parent.id !== 'string' || typeof parent.scope !== 'string'
                || storageKey !== prefix + JSON.stringify([parent.scope, parent.id])
                || !Array.isArray(parent.files)
                || !parent.files.every(file => file && typeof file.key === 'string' && typeof file.name === 'string')) continue;
            const compact = JSON.stringify({ ...parent, files: parent.files.map(file =>
                Object.fromEntries(fields.filter(field => field in file).map(field => [field, file[field]]))) });
            if (compact.length >= raw.length) continue;
            if (!backup.has(storageKey)) backup.set(storageKey, raw);
            localStorage.setItem(storageKey, compact);
            compacted++;
        } catch { skipped++; }
    }
    return { compacted, skipped, beforeKB: Math.round(before / 1024), afterKB: Math.round(size() / 1024) };
})()
