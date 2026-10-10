import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { CircleCheck as CheckCircleIcon, Download as DownloadIcon, Lock as LockIcon, Image as PhotoIcon, PlayCircle as PlayCircleIcon } from 'lucide-react';
import { useParams } from 'react-router-dom';
// public_bp moved to the dedicated `extras` container app (2026-09-17, see
// app.py's APP_ROLE=extras split) -- aliased so every call site below stays
// unchanged. resolveApiUrl already routes /public/* paths to the extras
// origin on its own (see apiClient.ts's EXTRAS_PATH_PREFIXES).
import { getExtras as get, postExtras as post, resolveApiUrl } from '../services/apiClient';
import { classifyApiError, isApiError, type ApiError } from '../services/apiError';
import { notifyApiError } from '../services/requestFeedback';
import { useBackendRecoveryRetry } from '../services/useBackendRecoveryRetry';
import { getBackendStatusSnapshot } from '../services/backendStatus';
import { showToast } from '../services/toast';
import { useDragSelect } from '../services/useDragSelect';
import PhotoViewer, { getMainMediaPath } from './shared/PhotoViewer';
import { Logo, LogoLockup } from './shared/Logo';
import { EmptyState } from './shared/EmptyState';
import { ErrorState } from './shared/ErrorState';
import { isVideoFilename } from '../utils/photoDisplay';

interface PublicPhoto {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    previewUrl?: string;
    rawFullPreviewUrl?: string;
    rotation?: number;
    thumbnailRotation?: number;
}

interface PublicAlbum {
    name: string;
    photoCount: number;
}

// Shared inline "Loading…" indicator that matches the app's Spinner (see the
// prototype's bits.tsx / prototype.css .pt-loading). Kept local so this
// standalone public page needn't reach into the prototype component tree.
const InlineSpinner: React.FC<{ label: string }> = ({ label }) => (
    <div className="pt-loading center" role="status" aria-live="polite">
        <span className="pt-spinner" aria-hidden="true" />
        <span>{label}</span>
    </div>
);

// The served thumbnail bakes in only the client's auto-orientation
// (thumbnailRotation); the user's manual rotate lives in `rotation` and is
// applied on top as a CSS transform -- mirrors PhotoGrid/PhotoTile so a grid
// tile matches what the viewer shows. The 0.74 down-scale on quarter-turns
// keeps a rotated landscape thumbnail from overflowing its square.
const normalizeRotation = (value?: number): number => {
    const rotation = Number(value || 0) % 360;
    return rotation < 0 ? rotation + 360 : rotation;
};
const tileRotationStyle = (photo: PublicPhoto): React.CSSProperties | undefined => {
    const remaining = normalizeRotation(normalizeRotation(photo.rotation) - normalizeRotation(photo.thumbnailRotation));
    if (!remaining) {
        return undefined;
    }
    return { transform: `rotate(${remaining}deg) scale(${remaining % 180 === 0 ? 1 : 0.74})` };
};

const resolveMediaSrc = (url?: string): string => {
    if (!url) {
        return '';
    }
    return url.startsWith('http') ? url : resolveApiUrl(url);
};

// Thumbnails are cheap (small, day-stable SAS/proxy URLs) so warming them all
// up front makes scrolling feel instant instead of waiting on native
// loading="lazy" as each tile enters the viewport (see PhotoTile). Concurrency
// is capped to stay under a browser's per-host connection limit rather than
// firing hundreds of Image() loads at once.
const BUFFER_CONCURRENCY = 6;
// Caps how long the grid stays hidden behind the buffering screen. Prefetch
// keeps running past this point (see the effect below) -- this only bounds
// how long a large or slow-to-load album blocks the initial reveal.
const BUFFER_REVEAL_TIMEOUT_MS = 6000;

// Full-size previews/originals can be multi-MB each (unlike thumbnails), so
// eagerly warming every photo in a large album would mean downloading
// hundreds of MB in the background for visitors who only open a handful.
// Warming just the first screen's worth covers the common case -- clicking
// one of the first photos -- while PhotoViewer's own neighbor preload (see
// PRELOAD_NEIGHBOR_COUNT there) takes over once the viewer is open.
const PREVIEW_PREFETCH_COUNT = 20;
const PREVIEW_PREFETCH_CONCURRENCY = 3;
const PUBLIC_ALBUM_PAGE_SIZE = 120;

const parsePublicAlbumError = (err: unknown): Record<string, unknown> => {
    // requestJson() always throws a classified ApiError, never the raw axios
    // error or JSON body — the structured fields the backend sent (e.g.
    // `codeRequired`, `retryAfterSeconds`) only survive on `responseData`.
    if (isApiError(err)) {
        const data = err.responseData;
        if (typeof data === 'object' && data !== null) {
            const payload = data as Record<string, unknown>;
            return typeof payload.error === 'string' ? payload : { ...payload, error: err.message };
        }
        return { error: err.message };
    }
    if (typeof err === 'object' && err !== null) {
        return err as Record<string, unknown>;
    }
    if (typeof err === 'string') {
        try {
            const parsed = JSON.parse(err);
            return typeof parsed === 'object' && parsed !== null ? parsed as Record<string, unknown> : { error: err };
        } catch {
            return { error: err };
        }
    }
    return {};
};

// Writes a plain status message into a popup window opened for the bulk
// download (see handleDownload below). It has no app bundle/styles of its
// own -- it's either about:blank or, once form.submit() navigates it, the
// download response -- so this is deliberately a tiny self-contained document.
const writeToPopup = (popup: Window | null, heading: string, detail: string): void => {
    if (!popup) {
        return;
    }
    popup.document.open();
    popup.document.write(
        '<!doctype html><html><head><title>Keepsake download</title>'
        + '<meta name="viewport" content="width=device-width, initial-scale=1">'
        + '<style>body{display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;'
        + 'font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#333;background:#fafafa;'
        + 'text-align:center;padding:24px;box-sizing:border-box}'
        + 'p{margin:4px 0}p:first-child{font-weight:600}</style></head>'
        + `<body><div><p>${heading}</p><p>${detail}</p></div></body></html>`,
    );
    popup.document.close();
};

const PublicAlbumPage: React.FC = () => {
    const { token } = useParams();
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<string>('');
    const [loadError, setLoadError] = useState<ApiError | null>(null);
    const [retryAfterSeconds, setRetryAfterSeconds] = useState<number | null>(null);
    const [codeRequired, setCodeRequired] = useState<boolean>(false);
    const [accessCode, setAccessCode] = useState<string>('');
    const [album, setAlbum] = useState<PublicAlbum | null>(null);
    const [photos, setPhotos] = useState<PublicPhoto[]>([]);
    const [offset, setOffset] = useState<number>(0);
    const [hasMore, setHasMore] = useState<boolean>(false);
    const [loadingMore, setLoadingMore] = useState<boolean>(false);
    const [viewerIndex, setViewerIndex] = useState<number | null>(null);
    const [selectedPhotos, setSelectedPhotos] = useState<Set<string>>(new Set());
    const [selectMode, setSelectMode] = useState<boolean>(false);
    const [downloading, setDownloading] = useState<boolean>(false);
    const downloadFormRef = useRef<HTMLFormElement | null>(null);
    // The scrollable region is the app-shell body (overflow-y:auto), not the
    // document -- so scroll save/restore below targets this element, not window.
    const bodyRef = useRef<HTMLDivElement | null>(null);
    // bufferReady gates the initial grid reveal; bufferPercent keeps updating
    // past that point so the ongoing background prefetch can still surface a
    // running percentage (see the meta line below) until it finishes.
    const [bufferReady, setBufferReady] = useState<boolean>(true);
    const [bufferPercent, setBufferPercent] = useState<number>(100);
    const bufferedPhotoCountRef = useRef<number>(0);

    // The grid unmounts entirely while the viewer is open (see the
    // viewerIndex === null gate below), so the page collapses to the
    // viewer's own height and the browser clamps window scroll to 0.
    // Save the scroll position before opening and restore it on close,
    // in a layout effect so it lands before the browser paints the
    // remounted grid. If in-viewer navigation (prev/next/filmstrip) left
    // the user on a different photo than the one they opened, scroll to
    // and highlight that photo's tile instead of the original position.
    const preViewerScrollYRef = useRef<number>(0);
    const openedViewerIndexRef = useRef<number | null>(null);
    const lastViewerIndexRef = useRef<number | null>(null);
    const [returnHighlightFilename, setReturnHighlightFilename] = useState<string | null>(null);
    if (viewerIndex !== null) {
        lastViewerIndexRef.current = viewerIndex;
    }
    // `photos` only mirrored into a ref (not a dep below) so an unrelated
    // re-render while the viewer stays closed can't re-fire this effect and
    // redo the jump/highlight (or clobber the scroll restore) a second time.
    const latestPhotosRef = useRef(photos);
    latestPhotosRef.current = photos;
    useLayoutEffect(() => {
        if (viewerIndex !== null) {
            return;
        }
        const closedAtIndex = lastViewerIndexRef.current;
        const openedAtIndex = openedViewerIndexRef.current;
        openedViewerIndexRef.current = null;
        lastViewerIndexRef.current = null;
        if (closedAtIndex !== null && openedAtIndex !== null && closedAtIndex !== openedAtIndex) {
            const returnedToPhoto = latestPhotosRef.current[closedAtIndex];
            if (returnedToPhoto) {
                const tileEl = document.querySelector(`[data-tile-id="${CSS.escape(returnedToPhoto.filename)}"]`);
                tileEl?.scrollIntoView({ block: 'center' });
                setReturnHighlightFilename(returnedToPhoto.filename);
                return;
            }
        }
        if (bodyRef.current) {
            bodyRef.current.scrollTop = preViewerScrollYRef.current;
        } else {
            window.scrollTo(0, preViewerScrollYRef.current);
        }
    }, [viewerIndex]);
    useEffect(() => {
        if (!returnHighlightFilename) {
            return undefined;
        }
        const timer = window.setTimeout(() => setReturnHighlightFilename(null), 1800);
        return () => window.clearTimeout(timer);
    }, [returnHighlightFilename]);
    const openViewerAt = useCallback((index: number) => {
        preViewerScrollYRef.current = bodyRef.current?.scrollTop ?? window.scrollY;
        openedViewerIndexRef.current = index;
        setViewerIndex(index);
    }, []);

    const loadPublicAlbum = useCallback(async (code: string = '', nextOffset = 0, append = false) => {
            if (!token) {
                setError('Invalid album link.');
                setLoading(false);
                return;
            }
            if (append) {
                setLoadingMore(true);
            } else {
                setLoading(true);
                setError('');
                setLoadError(null);
                setRetryAfterSeconds(null);
                setOffset(0);
                setHasMore(false);
            }
            try {
                const albumPath = `/public/albums/${encodeURIComponent(token)}?offset=${nextOffset}&limit=${PUBLIC_ALBUM_PAGE_SIZE}`;
                // withCredentials: this endpoint Set-Cookies a signed "grant" for
                // code-protected albums so the (cookie-only, cross-origin) preview/
                // image/thumbnail proxy routes can verify the code was already
                // cleared -- <img src> can't carry the code itself. Without
                // credentials on this call the browser never stores that cookie
                // cross-origin, so every media request 404s and the lightbox
                // silently falls back to the thumbnail for every photo.
                const response = code.trim() && !append
                    ? await post(albumPath, { accessCode: code.trim(), offset: nextOffset, limit: PUBLIC_ALBUM_PAGE_SIZE }, { withCredentials: true })
                    : await get(albumPath, { withCredentials: true });

                if (!response || !response.album) {
                    setError('This public album link is invalid or no longer available.');
                    setLoading(false);
                    return;
                }

                const nextPhotos = Array.isArray(response.photos) ? response.photos : [];
                setAlbum(response.album || null);
                setPhotos((prev) => {
                    if (!append) {
                        return nextPhotos;
                    }
                    const seen = new Set(prev.map((photo) => photo.filename));
                    return [...prev, ...nextPhotos.filter((photo: PublicPhoto) => !seen.has(photo.filename))];
                });
                const loadedThrough = nextOffset + nextPhotos.length;
                setOffset(loadedThrough);
                setHasMore(Boolean(response.hasMore));
                if (!append) {
                    setSelectedPhotos(new Set());
                    setSelectMode(false);
                    bufferedPhotoCountRef.current = 0;
                }
                setCodeRequired(false);
            } catch (err) {
                if (append) {
                    notifyApiError(err, { context: 'Unable to load more shared album photos.', retry: () => { void loadPublicAlbum(accessCode, nextOffset, true); } });
                    return;
                }
                setLoadError(classifyApiError(err));
                const payload = parsePublicAlbumError(err);
                if (payload.codeRequired === true) {
                    setCodeRequired(true);
                    const retryAfter = Number(payload.retryAfterSeconds);
                    if (Number.isFinite(retryAfter) && retryAfter > 0) {
                        setRetryAfterSeconds(Math.floor(retryAfter));
                        setError(`This album is protected. Please wait ${Math.floor(retryAfter)}s before retrying.`);
                    } else {
                        setRetryAfterSeconds(null);
                        setError('This album is protected. Enter the access code to continue.');
                    }
                } else {
                    const errorMsg = typeof payload.error === 'string' ? payload.error : 'This public album link is invalid or no longer available.';
                    setCodeRequired(false);
                    setRetryAfterSeconds(null);
                    setError(errorMsg);
                }
            } finally {
                if (append) {
                    setLoadingMore(false);
                } else {
                    setLoading(false);
                }
            }
    }, [token, accessCode]);

    const loadMorePhotos = useCallback(() => {
        if (!hasMore || loadingMore || loading) {
            return;
        }
        void loadPublicAlbum(accessCode, offset, true);
    }, [accessCode, hasMore, loading, loadingMore, loadPublicAlbum, offset]);

    useBackendRecoveryRetry(loadError, () => { void loadPublicAlbum(accessCode); });

    // archive (the container app behind /public/albums) runs minReplicas: 0.
    // The initial GET above warms it, but a code-protected album then sits on
    // the access-code screen while the visitor reads/types the code -- long
    // enough (observed ~90s) for archive to scale back to zero. The
    // access-code POST then has to cold-start it again, and Container Apps'
    // own ingress times out and returns a bare 503 (no app headers at all)
    // before the container finishes booting. Ping /health every 20s -- well
    // under the observed idle window -- for as long as the code screen is up,
    // so archive stays warm for the POST.
    //
    // No AbortSignal here: a real cold start is 20-30s+ (see httpClient.ts),
    // and a canceled request is deliberately excluded from the cold-start
    // retry loop there, so a short client-side timeout made every ping abort
    // mid-boot and never observe a successful wake -- the ping always
    // "failed" even though archive was coming up fine. Letting the ping run
    // through requestJson's own ~90s cold-start retry (and its GET dedup,
    // which coalesces a still-in-flight ping with the next interval's call
    // instead of stacking a second one) gives it a real chance to land.
    useEffect(() => {
        if (!codeRequired) {
            return undefined;
        }
        const ping = () => { void get('/health').catch(() => undefined); };
        const timer = window.setInterval(ping, 20000);
        return () => window.clearInterval(timer);
    }, [codeRequired]);

    // Warms every thumbnail into the browser's HTTP cache as soon as the photo
    // list arrives, instead of waiting for each tile's native loading="lazy"
    // to fire as it scrolls into view. Re-runs only when a fresh photo list
    // comes in (loadPublicAlbum always sets a brand-new array), not on
    // unrelated re-renders like selection toggling.
    useEffect(() => {
        if (photos.length === 0) {
            bufferedPhotoCountRef.current = 0;
            setBufferReady(true);
            setBufferPercent(100);
            return undefined;
        }
        const shouldGateReveal = bufferedPhotoCountRef.current === 0 || photos.length < bufferedPhotoCountRef.current;
        bufferedPhotoCountRef.current = photos.length;
        let cancelled = false;
        let settled = 0;
        const total = photos.length;
        if (shouldGateReveal) {
            setBufferReady(false);
            setBufferPercent(0);
        } else {
            setBufferReady(true);
        }

        const resolveThumbSrc = (url?: string): string => {
            if (!url) {
                return '';
            }
            return url.startsWith('http') ? url : resolveApiUrl(url);
        };

        const prefetchOne = (photo: PublicPhoto) => new Promise<void>((resolve) => {
            const src = resolveThumbSrc(photo.thumbnailUrl);
            if (!src) {
                resolve();
                return;
            }
            const img = new Image();
            img.onload = () => resolve();
            img.onerror = () => resolve();
            img.src = src;
        });

        const queue = [...photos];
        const runWorker = async () => {
            while (!cancelled) {
                const next = queue.shift();
                if (!next) {
                    return;
                }
                await prefetchOne(next);
                if (cancelled) {
                    return;
                }
                settled += 1;
                if (shouldGateReveal) {
                    setBufferPercent(Math.round((settled / total) * 100));
                }
            }
        };
        const workerCount = Math.min(BUFFER_CONCURRENCY, total);
        void Promise.all(Array.from({ length: workerCount }, runWorker)).then(() => {
            if (!cancelled && shouldGateReveal) {
                setBufferReady(true);
            }
        });

        const timeoutId = window.setTimeout(() => {
            if (!cancelled && shouldGateReveal) {
                setBufferReady(true);
            }
        }, BUFFER_REVEAL_TIMEOUT_MS);

        return () => {
            cancelled = true;
            window.clearTimeout(timeoutId);
        };
    }, [photos]);

    // Warms the full-size preview/original for the first screen's worth of
    // photos so opening one of them in the lightbox doesn't need a cold fetch.
    // Deferred until bufferReady so this heavier download doesn't compete with
    // (and slow down) the thumbnail buffering above; photos beyond this
    // window still get warmed on-demand by PhotoViewer's neighbor preload.
    useEffect(() => {
        if (!bufferReady || photos.length === 0) {
            return undefined;
        }
        let cancelled = false;
        const resolveSrc = (path: string) => (path.startsWith('http') ? path : resolveApiUrl(path));
        const queue = photos
            .slice(0, PREVIEW_PREFETCH_COUNT)
            .filter((photo) => !isVideoFilename(photo.filename))
            .map((photo) => getMainMediaPath(photo, true))
            .filter((path): path is string => Boolean(path));

        const prefetchOne = (path: string) => new Promise<void>((resolve) => {
            const img = new Image();
            img.onload = () => resolve();
            img.onerror = () => resolve();
            img.src = resolveSrc(path);
        });

        const runWorker = async () => {
            while (!cancelled) {
                const next = queue.shift();
                if (!next) {
                    return;
                }
                await prefetchOne(next);
            }
        };
        const workerCount = Math.min(PREVIEW_PREFETCH_CONCURRENCY, queue.length);
        void Promise.all(Array.from({ length: workerCount }, runWorker));

        return () => {
            cancelled = true;
        };
    }, [photos, bufferReady]);

    const selectedCount = selectedPhotos.size;
    const dragSelectHandlers = useDragSelect({
        isSelected: (filename) => selectedPhotos.has(filename),
        setSelected: (filename, selected) => {
            setSelectedPhotos(prev => {
                if (selected === prev.has(filename)) {
                    return prev;
                }
                const updated = new Set(prev);
                if (selected) {
                    updated.add(filename);
                } else {
                    updated.delete(filename);
                }
                return updated;
            });
        },
    });
    const downloadActionUrl = token
        ? resolveApiUrl(`/public/albums/${encodeURIComponent(token)}/download`)
        : '';
    const allSelected = selectedCount > 0 && selectedCount === photos.length;
    const toggleSelectAll = () => {
        setSelectedPhotos(allSelected ? new Set() : new Set(photos.map((photo) => photo.filename)));
    };
    const exitSelectMode = () => {
        setSelectMode(false);
        setSelectedPhotos(new Set());
    };
    const togglePhoto = (filename: string) => {
        setSelectedPhotos((current) => {
            const next = new Set(current);
            if (next.has(filename)) {
                next.delete(filename);
            } else {
                next.add(filename);
            }
            return next;
        });
    };

    const handleDownload = useCallback(async () => {
        const files = selectedCount > 0
            ? photos.filter((photo) => selectedPhotos.has(photo.filename))
            : photos;
        if (files.length === 0 || !token) {
            return;
        }

        // Open the destination tab synchronously, before any await below --
        // once an `await` breaks the call stack, Chrome/Safari no longer treat
        // a subsequent form.submit(target=_blank) as a direct result of this
        // click, and silently block the new tab as an unrequested popup (no
        // error, no toast: to the user the button just "does nothing").
        // Submitting the form at target=popupName later reuses this
        // already-open, already-user-activated window instead of trying to
        // spawn a new one.
        const popupName = `public-album-download-${Date.now()}`;
        const popup = window.open('', popupName);
        // The tab is otherwise blank white until download-check resolves and the
        // form navigates it -- on a cold-started backend that can take several
        // seconds, and a blank tab with no indication anything is happening
        // reads as broken (and, on failure, silently closing a tab the user
        // just watched open is its own confusing signal). Write a holding
        // message immediately, and an explicit one on failure instead of
        // closing it -- a real download success simply navigates over this.
        writeToPopup(popup, 'Preparing your download…', 'This tab will update automatically.');

        setDownloading(true);
        try {
            // form.submit() below gives no programmatic success/failure signal (it
            // opens a plain browser navigation in a new tab), so a dead backend
            // previously just opened a blank/failing tab with zero feedback. If the
            // backend is already known offline, skip straight to feedback instead of
            // waiting out another full cold-start retry cycle just to rediscover it.
            if (getBackendStatusSnapshot().status === 'offline') {
                writeToPopup(popup, "Can't reach the server right now.", 'Close this tab and try again once the album has reloaded.');
                showToast("Can't reach the server right now — try again once it's back.", {
                    variant: 'error',
                    action: { label: 'Retry', onClick: () => { void handleDownload(); } },
                });
                return;
            }
            await get(`/public/albums/${encodeURIComponent(token)}/download-check`);
            const form = downloadFormRef.current;
            if (!form) {
                popup?.close();
                return;
            }
            const filenamesInput = form.querySelector<HTMLInputElement>('input[name="filenames"]');
            if (filenamesInput) {
                filenamesInput.value = selectedCount > 0 ? JSON.stringify(files.map((photo) => photo.filename)) : '';
            }
            form.target = popupName;
            form.submit();
        } catch (err) {
            writeToPopup(popup, "Couldn't start the download.", 'Close this tab and try again from the album.');
            notifyApiError(err, { context: "Couldn't start the download", retry: () => { void handleDownload(); } });
        } finally {
            setDownloading(false);
        }
    }, [photos, selectedCount, selectedPhotos, token]);

    // Initial load only -- must NOT depend on loadPublicAlbum itself: that
    // callback closes over accessCode (see its own deps above), so its
    // identity changes on every keystroke in the passcode box. Depending on
    // it here re-ran this effect on every keystroke, each time flipping
    // `loading` true->false and unmounting/remounting the conditionally-
    // rendered passcode <input> (`!loading && codeRequired`) -- which closes
    // the on-screen keyboard on mobile the instant the focused input leaves
    // the DOM. Keyed on `token` (the actual "new album to load" signal)
    // instead, so retyping the code no longer re-triggers this at all.
    useEffect(() => {
        void loadPublicAlbum('');
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [token]);

    const countLabel = `${photos.length} photo${photos.length === 1 ? '' : 's'}`;
    const showToolbar = !loading && !error && bufferReady && photos.length > 0;

    return (
        <div className="pt-shell pt-public-shell">
            <div className="mock-stage">
                <div className="mock-viewport">
                    <div className="mock-app">
                        <header className="ios-header pt-public-header">
                            <LogoLockup size={30} className="ios-header-wordmark" />
                            <span className="pt-public-tag">Shared album</span>
                        </header>

                        <div className="mock-body pt-body" ref={bodyRef}>
                            {showToolbar && (
                                <div className="pt-toolbar">
                                    <div>
                                        <h1 className="pt-page-title">{album ? album.name : 'Shared album'}</h1>
                                        <p className="pt-page-sub">
                                            {countLabel}
                                            <span> · Read-only</span>
                                            {selectMode && selectedCount > 0 && <span> · {selectedCount} selected</span>}
                                            {bufferReady && bufferPercent < 100 && <span> · Caching {bufferPercent}%</span>}
                                        </p>
                                    </div>
                                    <div className="pt-toolbar-actions">
                                        {selectMode ? (
                                            <>
                                                <button type="button" className="btn" onClick={toggleSelectAll}>
                                                    {allSelected ? 'Clear' : 'Select all'}
                                                </button>
                                                <button type="button" className="btn" onClick={exitSelectMode}>
                                                    Done
                                                </button>
                                                <button
                                                    type="button"
                                                    className="btn mock-cta"
                                                    disabled={downloading || selectedCount === 0}
                                                    onClick={() => void handleDownload()}
                                                >
                                                    <DownloadIcon className="toolbar-icon" />
                                                    Download{selectedCount > 0 ? ` (${selectedCount})` : ''}
                                                </button>
                                            </>
                                        ) : (
                                            <>
                                                <button type="button" className="btn" onClick={() => setSelectMode(true)}>
                                                    Select
                                                </button>
                                                <button
                                                    type="button"
                                                    className="btn mock-cta"
                                                    disabled={downloading}
                                                    onClick={() => void handleDownload()}
                                                >
                                                    <DownloadIcon className="toolbar-icon" />
                                                    Download all
                                                </button>
                                            </>
                                        )}
                                    </div>
                                </div>
                            )}

                            <form
                                ref={downloadFormRef}
                                action={downloadActionUrl}
                                method="post"
                                style={{ display: 'none' }}
                            >
                                <input type="hidden" name="filenames" defaultValue="" />
                            </form>

                            {loading && (
                                <div className="pt-arrive pt-public-state">
                                    <InlineSpinner label="Loading shared album…" />
                                </div>
                            )}

                            {!loading && !error && !codeRequired && !bufferReady && photos.length > 0 && (
                                <div className="pt-arrive pt-public-buffering">
                                    <InlineSpinner label={`Loading photos… ${bufferPercent}%`} />
                                    <div className="progress-track">
                                        <div className="progress-bar" style={{ width: `${bufferPercent}%` }} />
                                    </div>
                                </div>
                            )}

                            {!loading && error && !codeRequired && (
                                <div className="pt-arrive pt-public-state">
                                    <ErrorState
                                        title="Album unavailable"
                                        message={error}
                                        onRetry={loadError?.retriable ? () => { void loadPublicAlbum(accessCode); } : undefined}
                                    />
                                </div>
                            )}

                            {!loading && codeRequired && (
                                <div className="pt-arrive pt-public-state">
                                    <div className="empty-state pt-public-lock">
                                        <span className="empty-state-icon" aria-hidden="true"><LockIcon /></span>
                                        <p className="empty-state-title">This album is protected</p>
                                        <p className="empty-state-message">{error || 'Enter the access code to continue.'}</p>
                                        <div className="pt-public-lock-form">
                                            <input
                                                id="public-album-access-code"
                                                type="password"
                                                className="field"
                                                placeholder="Access code"
                                                autoComplete="current-password"
                                                value={accessCode}
                                                onChange={(e) => setAccessCode(e.target.value)}
                                                onKeyDown={(e) => { if (e.key === 'Enter') void loadPublicAlbum(accessCode); }}
                                            />
                                            <button
                                                type="button"
                                                className="btn mock-cta"
                                                disabled={retryAfterSeconds !== null && retryAfterSeconds > 0}
                                                onClick={() => { void loadPublicAlbum(accessCode); }}
                                            >
                                                <LockIcon className="toolbar-icon" />
                                                Unlock
                                            </button>
                                        </div>
                                        {retryAfterSeconds !== null && retryAfterSeconds > 0 && (
                                            <p className="pt-page-sub">Retry available in {retryAfterSeconds}s.</p>
                                        )}
                                    </div>
                                </div>
                            )}

                            {!loading && !error && !codeRequired && photos.length === 0 && (
                                <div className="pt-arrive pt-public-state">
                                    <EmptyState icon={<PhotoIcon />} title="Nothing here yet" message="This shared album doesn't have any photos in it right now." />
                                </div>
                            )}

                            {!loading && !error && bufferReady && photos.length > 0 && viewerIndex === null && (
                                <div>
                                    <div className={`pt-grid pt-public-grid${selectMode ? ' select-mode' : ''}`}>
                                        {photos.map((photo, index) => {
                                            const isSelected = selectedPhotos.has(photo.filename);
                                            const isVideo = isVideoFilename(photo.filename);
                                            return (
                                                <div
                                                    key={photo.filename}
                                                    className={`pt-tile${isSelected ? ' selected' : ''}${photo.filename === returnHighlightFilename ? ' tile-return-highlight' : ''}`}
                                                    role="button"
                                                    tabIndex={0}
                                                    data-tile-id={photo.filename}
                                                    title={photo.filename}
                                                    onClick={() => { if (selectMode) { togglePhoto(photo.filename); } else { openViewerAt(index); } }}
                                                    onKeyDown={(e) => {
                                                        if (e.key === 'Enter' || e.key === ' ') {
                                                            e.preventDefault();
                                                            if (selectMode) { togglePhoto(photo.filename); } else { openViewerAt(index); }
                                                        }
                                                    }}
                                                >
                                                    <img
                                                        className="pt-tile-img"
                                                        src={resolveMediaSrc(photo.thumbnailUrl)}
                                                        alt={photo.filename}
                                                        loading="lazy"
                                                        draggable={false}
                                                        style={tileRotationStyle(photo)}
                                                    />
                                                    {isVideo && (
                                                        <span className="pt-tile-video" aria-hidden="true">
                                                            <PlayCircleIcon />
                                                        </span>
                                                    )}
                                                    {selectMode && (
                                                        <button
                                                            type="button"
                                                            className={`pt-tile-check${isSelected ? ' on' : ''}`}
                                                            aria-label={isSelected ? 'Deselect' : 'Select'}
                                                            aria-pressed={isSelected}
                                                            onClick={(e) => { e.stopPropagation(); togglePhoto(photo.filename); }}
                                                            onTouchStart={(e) => e.stopPropagation()}
                                                            {...dragSelectHandlers}
                                                        >
                                                            <CheckCircleIcon />
                                                        </button>
                                                    )}
                                                </div>
                                            );
                                        })}
                                    </div>
                                    {hasMore && (
                                        <div className="pt-public-state" style={{ minHeight: 'auto', padding: '18px 0 6px' }}>
                                            <button
                                                type="button"
                                                className="btn mock-cta"
                                                disabled={loadingMore}
                                                onClick={loadMorePhotos}
                                            >
                                                {loadingMore ? 'Loading…' : 'Load more photos'}
                                            </button>
                                        </div>
                                    )}
                                </div>
                            )}

                            {!loading && (
                                <footer className="pt-public-footer">
                                    <Logo size={18} />
                                    <span>Powered by <strong>Keepsake</strong> — your own private photo library</span>
                                </footer>
                            )}
                        </div>
                    </div>
                </div>
            </div>

            {viewerIndex !== null && (
                <PhotoViewer
                    photos={photos}
                    index={viewerIndex}
                    onClose={() => setViewerIndex(null)}
                    onIndexChange={setViewerIndex}
                    useProtectedMedia={false}
                />
            )}
        </div>
    );
};

export default PublicAlbumPage;
