import { useSyncExternalStore } from 'react';
import { get } from './apiClient';

/**
 * "Your library is being prepared" signal shared by every page that has to wait for the server's
 * indexes (gallery, search, people, albums, explore). Any page that sees the server say "still
 * building" calls ``reportIndexBuilding(source, true)``; one banner shows while any source is set.
 * A single poller watches /api/photos/index-status for the index-status-backed sources, then clears
 * those sources and tells listeners (pages refetch). People is retried by its own paged route, so it
 * contributes to the banner but is not cleared by the shared poller. The bar is deliberately a lump
 * sum: no percentages, just "working on it".
 */
type Listener = () => void;

const sources = new Set<string>();
const listeners = new Set<Listener>();
const readyListeners = new Set<() => void>();
let snapshot = false;
let timer: ReturnType<typeof setTimeout> | null = null;
let polling = false;
let tries = 0;

const POLL_MS = 8000;
const MAX_POLLS = 150;          // ~20 minutes; after that the banner stays until a page reports ready
// Sources the poller can resolve itself (they all mean "the server's library index isn't finished").
const POLLED = new Set(['session', 'gallery', 'search', 'albums', 'explore']);

const emit = () => {
    const next = sources.size > 0;
    if (next !== snapshot) {
        snapshot = next;
        listeners.forEach((l) => l());
    }
};

const poll = async () => {
    timer = null;
    if (polling) return;
    polling = true;
    try {
        const status = await get<{ ready?: boolean; building?: boolean }>('/api/photos/index-status').catch(() => null);
        if (status && status.ready && !status.building) {
            POLLED.forEach((s) => sources.delete(s));
            emit();
            readyListeners.forEach((cb) => cb());
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

const subscribe = (l: Listener) => {
    listeners.add(l);
    return () => { listeners.delete(l); };
};

export const useIndexBuilding = (): boolean => useSyncExternalStore(subscribe, () => snapshot, () => false);

export const __resetIndexBuildingForTests = (): void => {
    sources.clear();
    readyListeners.clear();
    if (timer) clearTimeout(timer);
    timer = null;
    polling = false;
    tries = 0;
    emit();
};
