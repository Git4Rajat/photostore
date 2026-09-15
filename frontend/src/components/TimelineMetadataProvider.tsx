import React, { createContext, useContext, useEffect, useState } from 'react';
import { get } from '../services/apiClient';
import { getActiveLibraryFromToken } from '../services/passwordAuthClient';
import { isApiError } from '../services/apiError';
import { isLikelyAuthenticated } from '../services/jobNotifications';
import type { TimelineSummary } from '../types/timeline';

type TimelineMetadataStatus = 'idle' | 'loading' | 'ready' | 'error';

interface TimelineMetadataContextValue {
    summary: TimelineSummary | null;
    status: TimelineMetadataStatus;
    error: string | null;
}

const TimelineMetadataContext = createContext<TimelineMetadataContextValue | null>(null);

export const useTimelineMetadata = (): TimelineMetadataContextValue => {
    const context = useContext(TimelineMetadataContext);
    if (!context) {
        throw new Error('useTimelineMetadata must be used inside TimelineMetadataProvider.');
    }
    return context;
};

const getFetchErrorMessage = (err: unknown): string => {
    if (isApiError(err)) {
        return err.message;
    }
    return err instanceof Error ? err.message : 'Unable to load the photo timeline.';
};

// Fetches the compact year/month/day photo-count summary once per SPA
// session (and again on library switch), and holds it in memory for the
// lifetime of the tab. This is the single source of truth the zoomable
// timeline renders from -- it must never be re-fetched on zoom/pan/hover.
export const TimelineMetadataProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [summary, setSummary] = useState<TimelineSummary | null>(null);
    const [status, setStatus] = useState<TimelineMetadataStatus>('idle');
    const [error, setError] = useState<string | null>(null);

    // Re-keying on the active library (rather than fetching once on mount)
    // means switching libraries fetches a fresh summary instead of leaking
    // library A's counts into library B -- the same scoping precedent
    // PhotoGallery's boot cache already follows (see photoCacheKey there).
    const activeLibrary = getActiveLibraryFromToken();

    useEffect(() => {
        // Cheap gate so the login screen and public/unauthenticated routes
        // (e.g. a shared album link) don't fire a doomed 401 on every load --
        // same check AppServicesProvider's job poller uses. Re-runs when
        // activeLibrary changes, which happens right after login produces a
        // real token, so this naturally fetches once auth completes.
        if (!isLikelyAuthenticated()) {
            setStatus('idle');
            return undefined;
        }
        let cancelled = false;
        setStatus('loading');
        setError(null);
        get<TimelineSummary>('/photos/timeline')
            .then((data) => {
                if (cancelled) {
                    return;
                }
                setSummary(data);
                setStatus('ready');
            })
            .catch((err) => {
                if (cancelled) {
                    return;
                }
                setSummary(null);
                setError(getFetchErrorMessage(err));
                setStatus('error');
            });
        return () => {
            cancelled = true;
        };
    }, [activeLibrary]);

    return (
        <TimelineMetadataContext.Provider value={{ summary, status, error }}>
            {children}
        </TimelineMetadataContext.Provider>
    );
};
