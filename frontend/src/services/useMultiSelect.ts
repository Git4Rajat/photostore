import { useCallback, useMemo, useState } from 'react';

/**
 * Generic Set-backed multi-select: toggle/select/clear/replace over a set of
 * string keys (filenames, album ids, face ids, etc). Extracted from the
 * near-identical `selectedPhotos`/`selectedAlbumIds` (Set<string>) state +
 * handlers duplicated across AlbumsPage/FaceClusters/ToolsPage.
 */
export const useMultiSelect = (initial: Iterable<string> = []) => {
    const [selected, setSelected] = useState<Set<string>>(() => new Set(initial));

    const toggle = useCallback((key: string) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(key)) {
                next.delete(key);
            } else {
                next.add(key);
            }
            return next;
        });
    }, []);

    const select = useCallback((key: string) => {
        setSelected((prev) => (prev.has(key) ? prev : new Set(prev).add(key)));
    }, []);

    const deselect = useCallback((key: string) => {
        setSelected((prev) => {
            if (!prev.has(key)) {
                return prev;
            }
            const next = new Set(prev);
            next.delete(key);
            return next;
        });
    }, []);

    const clear = useCallback(() => {
        setSelected((prev) => (prev.size === 0 ? prev : new Set()));
    }, []);

    const replaceAll = useCallback((keys: Iterable<string>) => {
        setSelected(new Set(keys));
    }, []);

    // Drops any selected keys that no longer exist in a fresh list (e.g.
    // after a delete/refresh) -- same "prune stale selection" logic
    // duplicated per-page against different item shapes.
    const pruneToLive = useCallback((liveKeys: Iterable<string>) => {
        const live = new Set(liveKeys);
        setSelected((prev) => {
            let changed = false;
            const next = new Set<string>();
            prev.forEach((key) => {
                if (live.has(key)) {
                    next.add(key);
                } else {
                    changed = true;
                }
            });
            return changed ? next : prev;
        });
    }, []);

    const isSelected = useCallback((key: string) => selected.has(key), [selected]);

    const selectedArray = useMemo(() => Array.from(selected), [selected]);

    return {
        selected,
        selectedArray,
        selectedCount: selected.size,
        isSelected,
        toggle,
        select,
        deselect,
        clear,
        replaceAll,
        pruneToLive,
        setSelected,
    };
};
