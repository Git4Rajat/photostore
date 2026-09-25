import React, { useEffect, useRef, useState } from 'react';
import { ArrowUpTrayIcon, PhotoIcon, UserPlusIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { useSimulatedUpload } from '../../useSimulatedUpload';
import PhotoGrid from '../components/PhotoGrid';

/** Gallery — the populated grid + drag-and-drop upload + the empty first-run state. */
export const GalleryPage: React.FC = () => {
    const { photos, addPhotos, navigate, uploadRequest, selectMany, clearSelection, selection, toast } = useStore();
    const { state, start, cancel, reset } = useSimulatedUpload();
    const [dragging, setDragging] = useState(false);
    const depth = useRef(0);
    const pending = useRef(0);
    const firstReq = useRef(uploadRequest);
    const gridRef = useRef<HTMLDivElement>(null);

    const selectVisible = () => {
        if (!gridRef.current) return;
        const container = gridRef.current;
        const containerRect = container.getBoundingClientRect();
        const tiles = container.querySelectorAll('.pt-tile');
        const visible: string[] = [];
        tiles.forEach((tile) => {
            const rect = tile.getBoundingClientRect();
            if (rect.bottom > containerRect.top && rect.top < containerRect.bottom) {
                const id = tile.getAttribute('data-photo-id');
                if (id) visible.push(id);
            }
        });
        if (visible.length > 0) selectMany(visible);
    };

    const begin = (count: number) => {
        pending.current = count;
        reset();
        start(count, 0.02);
    };

    // Topbar / empty-state upload button routes through the store's request signal.
    useEffect(() => {
        if (uploadRequest !== firstReq.current) begin(512);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [uploadRequest]);

    // When a run completes, the photos land in the grid.
    useEffect(() => {
        if (state.complete && pending.current > 0) {
            const added = addPhotos(pending.current - state.failedCount);
            toast(`Added ${added.length} photo${added.length > 1 ? 's' : ''}`);
            pending.current = 0;
        }
    }, [state.complete, state.failedCount, addPhotos, toast]);

    const onDrop = (e: React.DragEvent) => {
        e.preventDefault();
        depth.current = 0;
        setDragging(false);
        const n = e.dataTransfer?.files?.length || 24;
        begin(n);
    };

    const pct = state.total ? Math.round((state.done / state.total) * 100) : 0;

    if (photos.length === 0 && !state.running) {
        return (
            <div className="pt-arrive">
                <div className="empty-state">
                    <span className="empty-state-icon"><PhotoIcon /></span>
                    <p className="empty-state-title">This library is empty</p>
                    <p className="empty-state-message">
                        Upload your first photos and Keepsake starts sorting faces, places and moments as they come in.
                    </p>
                    <div className="empty-state-action pt-arrive-actions">
                        <button type="button" className="btn mock-cta" onClick={() => begin(120)}>
                            <ArrowUpTrayIcon className="toolbar-icon" /> Upload photos
                        </button>
                        <button type="button" className="btn" onClick={() => navigate('sharing')}>
                            <UserPlusIcon className="toolbar-icon" /> Invite family
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Gallery</h1>
                    <p className="pt-page-sub">{photos.length} photos</p>
                </div>
                <div className="pt-toolbar-actions">
                    {selection.length === 0 ? (
                        <button type="button" className="btn" onClick={selectVisible}>
                            Select visible
                        </button>
                    ) : (
                        <button type="button" className="btn" onClick={clearSelection}>
                            Clear
                        </button>
                    )}
                </div>
            </div>

            {(state.running || (state.complete && state.failedCount > 0)) && (
                <div className={`upload-dock${state.complete ? ' is-done' : ''}`}>
                    {state.running ? (
                        <>
                            <span className="count">Uploading {state.done} / {state.total}</span>
                            <span className="track"><span className="fill" style={{ width: `${pct}%` }} /></span>
                            <span className="rate">{state.mbps.toFixed(1)} MB/s</span>
                            <button type="button" className="btn" onClick={() => { cancel(); pending.current = 0; }}>Cancel</button>
                        </>
                    ) : (
                        <>
                            <span className="done-label">Uploaded {state.total - state.failedCount} of {state.total}</span>
                            <span className="track"><span className="fill" style={{ width: '100%' }} /></span>
                            <span className="failed">{state.failedCount} failed</span>
                            <button type="button" className="btn" onClick={reset}>Dismiss</button>
                        </>
                    )}
                </div>
            )}

            <div
                className={`pt-drop-surface${dragging ? ' dragging' : ''}`}
                ref={gridRef}
                onDragEnter={(e) => { e.preventDefault(); depth.current += 1; setDragging(true); }}
                onDragOver={(e) => e.preventDefault()}
                onDragLeave={(e) => { e.preventDefault(); depth.current = Math.max(0, depth.current - 1); if (!depth.current) setDragging(false); }}
                onDrop={onDrop}
            >
                <PhotoGrid photos={photos} gridRef={gridRef} />
                <div className="pt-drop-overlay">
                    <span className="drop-icon"><ArrowUpTrayIcon /></span>
                    <b>Drop to add to Keepsake</b>
                    <span className="sub">Release to start uploading</span>
                </div>
            </div>
        </div>
    );
};

export default GalleryPage;
