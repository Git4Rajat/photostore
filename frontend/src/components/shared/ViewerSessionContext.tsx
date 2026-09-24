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

interface ViewerSessionContextValue {
    session: ViewerSession | null;
    publishViewerSession: (session: ViewerSession | null) => void;
}

const ViewerSessionContext = createContext<ViewerSessionContextValue | null>(null);

export const ViewerSessionProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [session, setSession] = useState<ViewerSession | null>(null);
    const publishViewerSession = useCallback((next: ViewerSession | null) => setSession(next), []);
    return (
        <ViewerSessionContext.Provider value={{ session, publishViewerSession }}>
            {children}
        </ViewerSessionContext.Provider>
    );
};

export const useViewerSession = (): ViewerSessionContextValue => {
    const ctx = useContext(ViewerSessionContext);
    if (!ctx) {
        throw new Error('useViewerSession must be used within a ViewerSessionProvider');
    }
    return ctx;
};
