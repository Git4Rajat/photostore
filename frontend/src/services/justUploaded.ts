import { sessionScope } from './sessionScope';

/**
 * Session-only list of photos this browser just finished uploading, so the
 * gallery can show them before the (expensive) indexes cover them.
 *
 * Handover to the real index is the part that must never lose a photo: an entry
 * leaves only when the index is CONFIRMED to contain its filename (see
 * settleJustUploaded), or when it is explicitly removed (delete) or hard-expired.
 * A failed or partial lookup keeps the entry.
 */

export interface JustUploadedEntry {
    filename: string;
    captureDate: string | null;
    /** Local `blob:` thumbnail drawn in the browser; absent for formats it cannot decode. */
    thumbnailUrl?: string;
    addedAt: number;
}

type Stored = JustUploadedEntry & { scope: string };

export const JUST_UPLOADED_TTL_MS = 6 * 60 * 60 * 1000;
export const JUST_UPLOADED_MAX = 500;
const LOCAL_THUMB_EDGE = 240;
const LOCAL_THUMB_MAX_SOURCE_BYTES = 40 * 1024 * 1024;

const entries = new Map<string, Stored>();
const listeners = new Set<() => void>();
let snapshot: JustUploadedEntry[] = [];
let snapshotScope = '';
let version = 0;
let snapshotVersion = -1;

// Live Photos upload as a still plus a short clip that the server later attaches to the still
// (backend/live_photos.py). The browser cannot read Apple's pairing tags at finalize, so it
// uses the backend's base-name rule as a hint: a clip whose still is present gets no tile of
// its own. Worst case a real video sharing a still's name is shown a little late.
const STILL_EXTENSIONS = new Set(['heic', 'heif', 'hif', 'jpg', 'jpeg']);
const CLIP_EXTENSIONS = new Set(['mov', 'mp4', 'm4v']);
const extensionOf = (filename: string): string => (filename.includes('.') ? filename.slice(filename.lastIndexOf('.') + 1).toLowerCase() : '');
export const liveBaseStem = (filename: string): string => {
    const dot = filename.lastIndexOf('.');
    const stem = dot > 0 ? filename.slice(0, dot) : filename;
    return stem.replace(/-[0-9a-f]{8}$/, '').replace(/ \(\d+\)$/, '');
};
export const isLiveStillName = (filename: string): boolean => STILL_EXTENSIONS.has(extensionOf(filename));
export const isLiveClipName = (filename: string): boolean => CLIP_EXTENSIONS.has(extensionOf(filename));

const keyOf = (scope: string, filename: string): string => `${scope}\u0000${filename}`;
const emit = (): void => { version += 1; listeners.forEach((l) => l()); };
const revoke = (entry: Stored): void => {
    if (entry.thumbnailUrl?.startsWith('blob:')) {
        try { URL.revokeObjectURL(entry.thumbnailUrl); } catch { /* not a real object URL */ }
    }
};

// Runs from the snapshot getter (during render), so it only bumps the version:
// notifying listeners there would set state while rendering.
const expire = (now: number): void => {
    for (const [key, entry] of Array.from(entries)) {
        if (now - entry.addedAt > JUST_UPLOADED_TTL_MS) {
            revoke(entry);
            entries.delete(key);
            version += 1;
        }
    }
};

export const addJustUploaded = (entry: { filename: string; captureDate?: string | null; thumbnailUrl?: string }, now = Date.now()): void => {
    const scope = sessionScope();
    const key = keyOf(scope, entry.filename);
    const previous = entries.get(key);
    if (previous) {
        // Re-adding keeps the first position and any thumbnail already drawn.
        if (entry.thumbnailUrl && !previous.thumbnailUrl) {
            previous.thumbnailUrl = entry.thumbnailUrl;
            emit();
        } else if (entry.thumbnailUrl && entry.thumbnailUrl !== previous.thumbnailUrl) {
            revoke({ ...previous, thumbnailUrl: entry.thumbnailUrl });
        }
        return;
    }
    const stem = liveBaseStem(entry.filename);
    const mine0 = Array.from(entries.values()).filter((e) => e.scope === scope);
    if (isLiveClipName(entry.filename) && mine0.some((e) => isLiveStillName(e.filename) && liveBaseStem(e.filename) === stem)) return;
    if (isLiveStillName(entry.filename)) {
        // The clip finalized first and already has a tile: it belongs to this still.
        removeJustUploaded(mine0.filter((e) => isLiveClipName(e.filename) && liveBaseStem(e.filename) === stem).map((e) => e.filename));
    }
    entries.set(key, { scope, filename: entry.filename, captureDate: entry.captureDate ?? null, thumbnailUrl: entry.thumbnailUrl, addedAt: now });
    // Bounded: a huge burst drops the OLDEST overlay rows, which the index will
    // cover first anyway. Nothing is lost server-side.
    const mine = Array.from(entries.values()).filter((e) => e.scope === scope);
    for (let i = 0; i < mine.length - JUST_UPLOADED_MAX; i += 1) {
        revoke(mine[i]);
        entries.delete(keyOf(scope, mine[i].filename));
    }
    emit();
};

/** Attach a thumbnail drawn after the entry was added (drawing is async). */
export const setJustUploadedThumbnail = (filename: string, thumbnailUrl: string): void => {
    const entry = entries.get(keyOf(sessionScope(), filename));
    if (!entry) {
        // Removed (index took over, or deleted) while the thumbnail was drawing.
        try { URL.revokeObjectURL(thumbnailUrl); } catch { /* ignore */ }
        return;
    }
    if (entry.thumbnailUrl) {
        try { URL.revokeObjectURL(thumbnailUrl); } catch { /* ignore */ }
        return;
    }
    entry.thumbnailUrl = thumbnailUrl;
    emit();
};

export const removeJustUploaded = (filenames: Iterable<string>): void => {
    const scope = sessionScope();
    let changed = false;
    for (const filename of Array.from(filenames)) {
        const key = keyOf(scope, filename);
        const entry = entries.get(key);
        if (entry) {
            revoke(entry);
            entries.delete(key);
            changed = true;
        }
    }
    if (changed) emit();
};

export const listJustUploaded = (now = Date.now()): JustUploadedEntry[] => {
    expire(now);
    const scope = sessionScope();
    if (snapshotVersion === version && snapshotScope === scope) return snapshot;
    // Newest first, so the latest upload is the first tile.
    snapshot = Array.from(entries.values()).filter((e) => e.scope === scope).sort((a, b) => b.addedAt - a.addedAt);
    snapshotScope = scope;
    snapshotVersion = version;
    return snapshot;
};

export const subscribeJustUploaded = (listener: () => void): (() => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
};

/**
 * Handover: ask the real index which of the current entries it now has, and
 * drop exactly those. `lookup` resolves to the subset of names the index
 * contains, or null when it cannot answer; in that case, and when it throws,
 * nothing is dropped (the photo stays visible rather than vanishing).
 */
export const settleJustUploaded = async (
    lookup: (filenames: string[]) => Promise<ReadonlySet<string> | null>,
): Promise<number> => {
    const scope = sessionScope();
    const names = listJustUploaded().map((e) => e.filename);
    if (!names.length) return 0;
    let present: ReadonlySet<string> | null;
    try {
        present = await lookup(names);
    } catch {
        return 0;
    }
    if (!present || present.size === 0) return 0;
    // A scope change while the lookup ran must not drop another library's rows.
    if (sessionScope() !== scope) return 0;
    const confirmed = names.filter((n) => present!.has(n));
    removeJustUploaded(confirmed);
    return confirmed.length;
};

let drawing: Promise<unknown> = Promise.resolve();

/**
 * Draws a small thumbnail in the browser, one at a time so an upload burst does
 * not stack decodes. Resolves undefined for anything the browser cannot decode
 * (HEIC/RAW/video on most browsers) or that is too large to be worth it.
 */
export const drawLocalThumbnail = (file: Blob): Promise<string | undefined> => {
    const task = drawing.then(async () => {
        if (typeof createImageBitmap !== 'function' || typeof document === 'undefined') return undefined;
        if (!file.type.startsWith('image/') || file.size > LOCAL_THUMB_MAX_SOURCE_BYTES) return undefined;
        try {
            const bitmap = await createImageBitmap(file);
            try {
                const scale = Math.min(1, LOCAL_THUMB_EDGE / Math.max(bitmap.width, bitmap.height));
                const canvas = document.createElement('canvas');
                canvas.width = Math.max(1, Math.round(bitmap.width * scale));
                canvas.height = Math.max(1, Math.round(bitmap.height * scale));
                const ctx = canvas.getContext('2d');
                if (!ctx) return undefined;
                ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
                const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.7));
                return blob ? URL.createObjectURL(blob) : undefined;
            } finally {
                bitmap.close?.();
            }
        } catch {
            return undefined;
        }
    });
    drawing = task.catch(() => undefined);
    return task;
};

export const __resetJustUploadedForTests = (): void => {
    entries.clear();
    listeners.clear();
    snapshot = [];
    snapshotScope = '';
    snapshotVersion = -1;
    version = 0;
    drawing = Promise.resolve();
};
