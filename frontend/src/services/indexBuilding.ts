import { useSyncExternalStore } from 'react';
import { get } from './apiClient';

/**
 * Index building status service. Tracks whether indexes are being built and provides
 * per-index status details. The poller watches /api/tools/indexes/status for detailed
 * status including which specific indexes are ready/building/dirty.
 *
 * Exports:
 * - useIndexBuilding() - returns true if any index is building (for banner)
 * - useIndexStatus() - returns detailed per-index status {sort, lexical, albums, people}
 * - reportIndexBuilding(source, bool) - pages report when they need to wait
 * - onIndexReady() - callback when all indexes become ready
 */

type Listener = () => void;
type IndexKind = 'sort' | 'lexical' | 'albums' | 'people';

interface IndexDetail {
  ready: boolean;
  dirty: boolean;
  updated_at?: string;
}

interface IndexStatus {
  [key: string]: IndexDetail;
}

const sources = new Set<string>();
const listeners = new Set<Listener>();
const readyListeners = new Set<() => void>();
let isBuilding = false;
let indexStatus: IndexStatus = { sort: { ready: false, dirty: false }, lexical: { ready: false, dirty: false }, albums: { ready: false, dirty: false }, people: { ready: false, dirty: false } };
let statusListeners = new Set<Listener>();
let timer: ReturnType<typeof setTimeout> | null = null;
let polling = false;
let tries = 0;

const POLL_MS = 8000;
const MAX_POLLS = 150;          // ~20 minutes; after that the banner stays until a page reports ready
// Sources the poller can resolve itself (they all mean "the server's library index isn't finished").
const POLLED = new Set(['session', 'gallery', 'search', 'albums', 'explore']);

const emit = () => {
    const next = sources.size > 0;
    if (next !== isBuilding) {
        isBuilding = next;
        listeners.forEach((l) => l());
    }
};

const emitStatus = () => {
    statusListeners.forEach((l) => l());
};

const poll = async () => {
    timer = null;
    if (polling) return;
    polling = true;
    try {
        const status = await get<{ ready?: boolean; building?: boolean; indexes?: IndexStatus }>('/api/tools/indexes/status').catch(() => null);
        if (status) {
            // Update detailed index status
            if (status.indexes) {
                indexStatus = status.indexes as IndexStatus;
                emitStatus();
            }
            // If all indexes ready and not building, clear polling sources
            if (status.ready && !status.building) {
                POLLED.forEach((s) => sources.delete(s));
                emit();
                readyListeners.forEach((cb) => cb());
            }
        }
    } finally {
        polling = false;
    }
    const waiting = Array.from(sources).some((s) => POLLED.has(s));
    if (waiting && tries < MAX_POLLS) {
        tries += 1;
        timer = setTimeout(() => { void poll(); }, POLL_MS);
    }
};

export const reportIndexBuilding = (source: string, building: boolean): void => {
    const had = sources.has(source);
    if (building) sources.add(source); else sources.delete(source);
    if (had !== building) emit();
    if (building && POLLED.has(source) && timer === null && !polling) {
        tries = 0;
        timer = setTimeout(() => { void poll(); }, POLL_MS);
    }
};

/** Called once when a build the poller was watching finishes. Returns an unsubscribe function. */
export const onIndexReady = (cb: () => void): (() => void) => {
    readyListeners.add(cb);
    return () => { readyListeners.delete(cb); };
};

/** Get current per-index status. Returns {sort, lexical, albums, people} with ready/dirty/updated_at. */
export const getCurrentIndexStatus = (): IndexStatus => ({ ...indexStatus });

const subscribe = (l: Listener) => {
    listeners.add(l);
    return () => { listeners.delete(l); };
};

const subscribeStatus = (l: Listener) => {
    statusListeners.add(l);
    return () => { statusListeners.delete(l); };
};

export const useIndexBuilding = (): boolean => useSyncExternalStore(subscribe, () => isBuilding, () => false);

/** Subscribe to per-index status updates. Returns current status and unsubscribe function. */
export const useIndexStatus = (): IndexStatus => useSyncExternalStore(subscribeStatus, () => ({ ...indexStatus }), () => indexStatus);

export const __resetIndexBuildingForTests = (): void => {
    sources.clear();
    readyListeners.clear();
    statusListeners.clear();
    if (timer) clearTimeout(timer);
    timer = null;
    polling = false;
    tries = 0;
    isBuilding = false;
    indexStatus = { sort: { ready: false, dirty: false }, lexical: { ready: false, dirty: false }, albums: { ready: false, dirty: false }, people: { ready: false, dirty: false } };
    emit();
};
