import { getLocalAlbumsIndex } from './localAlbumsIndex';
import { getMediaToken } from './mediaToken';
import { getLocalSortIndex } from './localSortIndex';

/**
 * Session-start preload of every client-side index, fetched concurrently (the
 * sort index gates the gallery; albums back that page). Each loader persists
 * to IndexedDB keyed by the server's sourceVersion, so repeat visits resolve
 * from disk without re-downloading. Never throws: a failed preload just means
 * the first consumer pays for the load, as before.
 */
let started: Promise<void> | null = null;

export const preloadLocalIndexes = (): Promise<void> => {
    if (!started) {
        started = Promise.allSettled([
            getMediaToken(), getLocalSortIndex(), getLocalAlbumsIndex(),
        ])
            .then(() => undefined)
            .finally(() => {
                started = null;
            });
    }
    return started;
};
