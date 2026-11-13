import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Download as DownloadIcon } from 'lucide-react';
import ViewerShell from './ViewerShell';
import { isVideoFilename } from '../../../utils/photoDisplay';
import { resolveApiUrl } from '../../../services/apiClient';
import { showToast } from '../../../services/toast';

export interface PublicViewerPhoto {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    previewUrl?: string;
    rawFullPreviewUrl?: string;
    rotation?: number;
}

// The page is served relative (/public/...) URLs; the <img>/<video> must hit the API host.
const resolveSrc = (url?: string): string => (!url ? '' : url.startsWith('http') ? url : resolveApiUrl(url));

/** The shared album's lightbox: the Gallery viewer's chrome (see ViewerShell)
 *  over a read-only, signed-URL photo list -- no store, no auth, no rating,
 *  albums or delete. Media comes straight from the URLs the page was served. */
const PublicPhotoViewer: React.FC<{
    photos: PublicViewerPhoto[];
    index: number;
    onClose: () => void;
    onIndexChange: (next: number) => void;
}> = ({ photos, index, onClose, onIndexChange }) => {
    const photo = photos[index];
    const [fullRes, setFullRes] = useState(false);
    useEffect(() => { setFullRes(false); }, [photo?.filename]);

    const onStep = useCallback((delta: number) => {
        const next = index + delta;
        if (next >= 0 && next < photos.length) onIndexChange(next);
    }, [index, photos.length, onIndexChange]);

    // Warm the neighbours' previews so stepping through is not a cold fetch each time.
    useEffect(() => {
        [photos[index - 1], photos[index + 1]].forEach((p) => {
            const src = p && !isVideoFilename(p.filename) ? (p.previewUrl || p.url) : '';
            if (src) { const img = new Image(); img.src = resolveSrc(src); }
        });
    }, [photos, index]);

    const isVideo = Boolean(photo && isVideoFilename(photo.filename));
    const mainSrc = useMemo(() => {
        if (!photo) return '';
        if (fullRes) return resolveSrc(photo.rawFullPreviewUrl || photo.url);
        return resolveSrc(photo.previewUrl || photo.url || photo.thumbnailUrl);
    }, [photo, fullRes]);

    const download = useCallback(async () => {
        if (!photo?.url) return;
        try {
            const response = await fetch(resolveSrc(photo.url), { mode: 'cors', credentials: 'omit' });
            if (!response.ok) throw new Error(String(response.status));
            const objectUrl = URL.createObjectURL(await response.blob());
            const anchor = document.createElement('a');
            anchor.href = objectUrl;
            anchor.download = photo.filename;
            anchor.rel = 'noreferrer';
            anchor.style.display = 'none';
            document.body.appendChild(anchor);
            anchor.click();
            anchor.remove();
            setTimeout(() => URL.revokeObjectURL(objectUrl), 5000);
        } catch {
            showToast('Download failed', { variant: 'error' });
        }
    }, [photo]);

    if (!photo) return null;

    return (
        <ViewerShell
            photoKey={photo.filename}
            filename={photo.filename}
            title={photo.filename}
            index={index}
            count={photos.length}
            initialRotation={photo.rotation}
            onClose={onClose}
            onStep={onStep}
            media={{ url: mainSrc, loading: false, progress: 0 }}
            videoSrc={isVideo ? resolveSrc(photo.url) : undefined}
            fullRes={fullRes}
            onToggleFullRes={() => setFullRes((v) => !v)}
            onFullResFailed={() => {
                setFullRes(false);
                showToast('This browser can’t show this photo’s original. Showing the preview instead.');
            }}
            showFullResControl={!isVideo}
            onDownloadOriginal={() => void download()}
            bar={(
                <>
                    <div className="pt-vb-group left" />
                    <div className="pt-vb-group center">
                        <button type="button" className="pt-vb-btn" onClick={() => void download()}>
                            <DownloadIcon /> <span>Download</span>
                        </button>
                    </div>
                    <div className="pt-vb-group right" />
                </>
            )}
        />
    );
};

export default PublicPhotoViewer;
