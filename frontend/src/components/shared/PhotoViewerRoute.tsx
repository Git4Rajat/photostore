import React, { useCallback } from 'react';
import { Navigate, useLocation, useNavigate, useParams } from 'react-router-dom';
import PhotoViewer from './PhotoViewer';
import { useViewerSession } from './ViewerSessionContext';

export const photoViewerPath = (filename: string): string => `/photo/${encodeURIComponent(filename)}`;

interface ViewerLocationState {
    background?: { pathname: string; search: string; hash: string };
}

// Mounted at /photo/:filename via the background-location pattern in
// App.tsx: the background <Routes> keeps rendering whichever page opened
// this (Gallery/Albums) against a frozen "background" location, so it never
// re-renders or unmounts -- this route only overlays the viewer on top. That
// means the grid's scroll position is never touched by opening/closing the
// viewer at all (nothing forces it to change), which is simpler and more
// robust than the previous same-page state-swap + manual scroll-restore.
//
// Requires both `state.background` (set by whoever pushed this route) and a
// currently-published ViewerSession (the photo list + action handlers from
// that host page, via ViewerSessionContext) to render the real viewer --
// missing either means this was reached cold (hard refresh, pasted/shared
// link, or the photo isn't in the current list), so it falls back to the
// existing single-photo deep-link path instead of a broken/list-less viewer.
const PhotoViewerRoute: React.FC = () => {
    const params = useParams<{ filename: string }>();
    const location = useLocation();
    const navigate = useNavigate();
    const { session } = useViewerSession();
    const background = (location.state as ViewerLocationState | null)?.background;
    const filename = params.filename ? decodeURIComponent(params.filename) : '';

    const handleClose = useCallback(() => {
        navigate(-1);
    }, [navigate]);

    const handleIndexChange = useCallback((nextIndex: number) => {
        const nextPhoto = session?.photos[nextIndex];
        if (!nextPhoto) {
            return;
        }
        navigate(photoViewerPath(nextPhoto.filename), { replace: true, state: { background } });
    }, [navigate, session, background]);

    if (!filename || !background || !session) {
        // Cold load (hard refresh, pasted/shared link) -- no host page is
        // mounted to have published a session. Fall back to the existing
        // single-photo deep-link path rather than a list-less viewer.
        return <Navigate to={`/?focus=${encodeURIComponent(filename)}`} replace />;
    }

    const index = session.photos.findIndex((photo) => photo.filename === filename);
    if (index === -1) {
        // In an active session but this filename isn't in the current list
        // anymore (e.g. it was just deleted, or a filter changed under us)
        // -- nothing sensible to show, so return to wherever this was opened
        // from instead of guessing at a neighbor.
        return <Navigate to={`${background.pathname}${background.search}${background.hash}`} replace />;
    }

    return (
        <div className="photo-viewer-route-overlay">
            <PhotoViewer
                photos={session.photos}
                index={index}
                onClose={handleClose}
                onIndexChange={handleIndexChange}
                useProtectedMedia
                onRotationSave={session.onRotationSave}
                onRate={session.onRate}
                onToggleLike={session.onToggleLike}
                onDelete={session.onDelete}
                onOpenActions={session.onOpenActions}
            />
        </div>
    );
};

export default PhotoViewerRoute;
