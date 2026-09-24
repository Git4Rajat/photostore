import React, { createContext, useCallback, useContext, useState } from 'react';
import type { ViewerPhoto } from './PhotoViewer';

// PhotoViewerRoute (mounted at /photo/:filename via the background-location
// pattern in App.tsx) is a route sibling of PhotoGallery/AlbumsPage, not a
// child -- it can't receive the current photo list or action handlers as
// props the way the old inline <PhotoViewer> mount did. Whichever host page
// is currently open publishes both here instead. Only one host is ever
// mounted at a time (they're alternate routes), so "the current session" is
// unambiguous.
export interface ViewerSession {
    photos: ViewerPhoto[];
    onRotationSave?: (filename: string, rotation: number) => Promise<void> | void;
    onRate?: (filename: string, rating: number) => Promise<void> | void;
    onToggleLike?: (filename: string) => Promise<void> | void;
    onDelete?: (filename: string) => void;
    onOpenActions?: (filename: string, initialScreen?: 'menu' | 'chooseAlbum') => void;
}

// Split into two contexts on purpose: Gallery/Albums both publish (via
// PublishContext) *and* would otherwise need to read from the same provider
// -- if session value and publish function shared one context, publishing a
// new session would re-render the publisher itself (it also subscribes to
// the context for the publish function), which re-runs its publish effect,
// which publishes again... an infinite render loop. PublishContext's value
// (the setter) never changes identity, so subscribing to it is a no-op for
// re-renders; only PhotoViewerRoute (a route sibling, never the publisher)
// subscribes to SessionContext, which does change on every publish.
const ViewerSessionContext = createContext<ViewerSession | null>(null);
const ViewerSessionPublishContext = createContext<((session: ViewerSession | null) => void) | null>(null);

export const ViewerSessionProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [session, setSession] = useState<ViewerSession | null>(null);
    const publishViewerSession = useCallback((next: ViewerSession | null) => setSession(next), []);
    return (
        <ViewerSessionPublishContext.Provider value={publishViewerSession}>
            <ViewerSessionContext.Provider value={session}>
                {children}
            </ViewerSessionContext.Provider>
        </ViewerSessionPublishContext.Provider>
    );
};

// For PhotoViewerRoute: reads the current session, re-renders when it changes.
// null is a legitimate value (no host page currently has one published), so
// there's nothing to validate here beyond what usePublishViewerSession below
// already guards.
export const useViewerSession = (): { session: ViewerSession | null } => ({
    session: useContext(ViewerSessionContext),
});

// For Gallery/Albums: gets the (referentially stable) publish function only,
// never re-renders when the session value itself changes.
export const usePublishViewerSession = (): (session: ViewerSession | null) => void => {
    const publish = useContext(ViewerSessionPublishContext);
    if (!publish) {
        throw new Error('usePublishViewerSession must be used within a ViewerSessionProvider');
    }
    return publish;
};
