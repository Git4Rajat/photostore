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
    /** Past the tile cap: no tile is drawn, but it still counts and still hands over by name. */
    compact?: boolean;
}

type Stored = JustUploadedEntry & { scope: string };

export const JUST_UPLOADED_TTL_MS = 6 * 60 * 60 * 1000;
/** Most overlay tiles drawn at once; entries beyond it are kept by name so the count stays right. */
export const JUST_UPLOADED_MAX = 500;
// Listeners hear about additions at most this often. Every notification re-renders the whole
// store, and an upload finalizes files far faster than the screen needs to change.
export const JUST_UPLOADED_NOTIFY_MS = 500;

const entries = new Map<string, Stored>();
const listeners = new Set<() => void>();
let snapshot: JustUploadedEntry[] = [];
let snapshotScope = '';
let version = 0;
let snapshotVersion = -1;
let lastNotifyAt = 0;
let notifyTimer: ReturnType<typeof setTimeout> | null = null;

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
const notify = (): void => {
    if (notifyTimer !== null) { clearTimeout(notifyTimer); notifyTimer = null; }
    lastNotifyAt = Date.now();
    listeners.forEach((l) => l());
};
// Leading edge fires at once (the first photo shows immediately); a burst after it is coalesced
// into one trailing notification. Removals pass `immediate`: they are rare and the user waits on them.
const emit = (immediate = false): void => {
    version += 1;
    if (immediate || Date.now() - lastNotifyAt >= JUST_UPLOADED_NOTIFY_MS) { notify(); return; }
    if (notifyTimer === null) notifyTimer = setTimeout(notify, JUST_UPLOADED_NOTIFY_MS - (Date.now() - lastNotifyAt));
};
export const flushJustUploaded = (): void => { if (notifyTimer !== null) notify(); };
const revokeUrl = (url: string | undefined): void => {
    if (url?.startsWith('blob:')) {
        try { URL.revokeObjectURL(url); } catch { /* not a real object URL */ }
    }
};
const revoke = (entry: Stored): void => revokeUrl(entry.thumbnailUrl);

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
    if (isLiveClipName(entry.filename) && mine0.some((e) => isLiveStillName(e.filename) && liveBaseStem(e.filename) === stem)) {
        revokeUrl(entry.thumbnailUrl);
        return;
    }
    if (isLiveStillName(entry.filename)) {
        // The clip finalized first and already has a tile: it belongs to this still.
        removeJustUploaded(mine0.filter((e) => isLiveClipName(e.filename) && liveBaseStem(e.filename) === stem).map((e) => e.filename));
    }
    entries.set(key, { scope, filename: entry.filename, captureDate: entry.captureDate ?? null, thumbnailUrl: entry.thumbnailUrl, addedAt: now });
    // Bounded tiles: a huge burst turns the OLDEST rows into name-only entries (no thumbnail,
    // no tile). They keep counting and still hand over to the index by name.
    const tiled = Array.from(entries.values()).filter((e) => e.scope === scope && !e.compact);
    for (let i = 0; i < tiled.length - JUST_UPLOADED_MAX; i += 1) {
        revoke(tiled[i]);
        tiled[i].thumbnailUrl = undefined;
        tiled[i].compact = true;
    }
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
    if (changed) emit(true);
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

export const __resetJustUploadedForTests = (): void => {
    entries.clear();
    listeners.clear();
    snapshot = [];
    snapshotScope = '';
    snapshotVersion = -1;
    version = 0;
    lastNotifyAt = 0;
    if (notifyTimer !== null) { clearTimeout(notifyTimer); notifyTimer = null; }
};
