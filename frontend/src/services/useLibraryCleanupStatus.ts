import { useEffect, useRef } from 'react';
import * as library from './libraryClient';
import { subscribeLibraryChanges } from './libraryChanges';

// Ordinary visits read core's metadata snapshot. Only a running cleanup needs
// archive reconciliation. Core polling also detects work started on another device.
export const useLibraryCleanupStatus = (
    libraryId: string | undefined,
    initialInfo: library.CleanupInfo | undefined,
    onCompleted: (info: library.CleanupInfo) => void,
): void => {
    const lastCompletion = useRef({ libraryId, at: 0 });
    useEffect(() => {
        if (!libraryId) return;
        if (lastCompletion.current.libraryId !== libraryId) lastCompletion.current = { libraryId, at: 0 };
        let info = initialInfo;
        let cancelled = false;
        let inFlight = false;
        let refreshPending = false;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const report = () => {
            if (info?.lastCleanupStatus === 'completed' && info.lastCleanupTime > lastCompletion.current.at) {
                lastCompletion.current.at = info.lastCleanupTime;
                onCompleted(info);
            }
        };
        const tick = async (refreshSnapshot = false) => {
            if (cancelled || document.hidden) return;
            if (inFlight) { refreshPending ||= refreshSnapshot; return; }
            inFlight = true;
            clearTimeout(timer);
            try {
                if (refreshSnapshot || info?.lastCleanupStatus !== 'in_progress') {
                    const mine = await library.getMine();
                    if (cancelled || mine.activeLibraryId !== libraryId) return;
                    info = mine.cleanupInfo;
                }
                if (!cancelled && !document.hidden && info?.lastCleanupStatus === 'in_progress') {
                    const latest = await library.getLibraryCleanupInfo();
                    if (cancelled) return;
                    info = latest;
                }
                if (!cancelled) report();
            } catch {
                // Preserve the last known state and retry while this view is visible.
            } finally {
                inFlight = false;
                if (!cancelled && !document.hidden) {
                    const pending = refreshPending;
                    refreshPending = false;
                    timer = setTimeout(() => { void tick(pending); }, pending ? 0 : info?.lastCleanupStatus === 'in_progress' ? 3000 : 30000);
                }
            }
        };
        const refresh = () => { void tick(true); };
        const visibility = () => {
            clearTimeout(timer);
            if (!document.hidden) refresh();
        };
        const unsubscribe = subscribeLibraryChanges((change) => {
            if (change.operation === 'library-cleanup') refresh();
        });
        window.addEventListener('focus', refresh);
        document.addEventListener('visibilitychange', visibility);
        report();
        timer = setTimeout(() => { void tick(); }, info?.lastCleanupStatus === 'in_progress' ? 0 : 30000);
        return () => {
            cancelled = true;
            clearTimeout(timer);
            unsubscribe();
            window.removeEventListener('focus', refresh);
            document.removeEventListener('visibilitychange', visibility);
        };
    }, [libraryId, initialInfo, onCompleted]);
};
