import { getLocalAlbumsIndex } from './localAlbumsIndex';
import { getLocalPeopleIndex } from './localPeopleIndex';
import { getLocalSearchIndex } from './localSearchIndex';
import { getLocalSortIndex } from './localSortIndex';

/**
 * Session-start preload of every client-side index, fetched concurrently (the
 * sort index gates the gallery; albums/people back those pages; the slim search
 * index backs Ask/search). Each loader persists to IndexedDB keyed by the server's
 * sourceVersion, so on a repeat visit this resolves from disk without
 * re-downloading. Never throws: a failed preload just means the first consumer
 * pays for the load, as before.
 */
let started: Promise<void> | null = null;

export const preloadLocalIndexes = (): Promise<void> => {
    if (!started) {
        started = Promise.allSettled([
            getLocalSortIndex(), getLocalAlbumsIndex(), getLocalPeopleIndex(), getLocalSearchIndex(),
        ])
            .then(() => undefined)
            .finally(() => {
                started = null;
            });
    }
    return started;
};
