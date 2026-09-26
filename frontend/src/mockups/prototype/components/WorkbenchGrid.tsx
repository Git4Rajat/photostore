import React, { useMemo, useState } from 'react';
import {
    CheckCircleIcon as CheckCircleSolid,
    MagnifyingGlassIcon,
} from '@heroicons/react/24/solid';
import {
    CameraIcon,
    DocumentMagnifyingGlassIcon,
    DocumentTextIcon,
    InformationCircleIcon,
    MapPinIcon,
    Square2StackIcon,
    SparklesIcon,
    UserIcon,
    XCircleIcon,
} from '@heroicons/react/24/outline';
import { usePhotoThumbnails, fetchPhotoMetadata } from '../media';
import type { PhotoMetadata } from '../media';
import type { Photo } from '../types';

export type WorkbenchStep = 'Preview' | 'Thumbnails' | 'EXIF' | 'OCR' | 'Vision' | 'Geo' | 'Faces';

export const WORKBENCH_STEPS: WorkbenchStep[] = ['Preview', 'Thumbnails', 'EXIF', 'OCR', 'Vision', 'Geo', 'Faces'];

const STEP_ICONS: Record<WorkbenchStep, React.ComponentType<React.SVGProps<SVGSVGElement>>> = {
    Preview: CameraIcon,
    Thumbnails: Square2StackIcon,
    EXIF: DocumentTextIcon,
    OCR: DocumentMagnifyingGlassIcon,
    Vision: SparklesIcon,
    Geo: MapPinIcon,
    Faces: UserIcon,
};

type StepStatus = 'done' | 'pending' | 'failed';

// Maps a WorkbenchStep to its field on Photo['processing'] -- mirrors
// ChipStepKey/processingServiceLabels in the (now-dead) legacy ToolsPage.tsx,
// the last place this per-photo/per-step data was rendered from real
// backend telemetry (photos.list already returns it -- see
// PHOTO_LIST_SELECT_FIELDS/_build_photo_summary in the backend).
const STEP_FIELDS: Record<WorkbenchStep, keyof NonNullable<Photo['processing']>> = {
    Preview: 'preview',
    Thumbnails: 'thumbnail',
    EXIF: 'exif',
    OCR: 'ocr',
    Vision: 'aiVision',
    Geo: 'mapDetection',
    Faces: 'face',
};

const DONE_STATUSES = new Set(['done', 'skipped', 'unsupported']);
const FAILED_STATUSES = new Set(['failed', 'timeout', 'no_data']);

const statusFor = (photo: Photo, step: WorkbenchStep): StepStatus => {
    const raw = photo.processing?.[STEP_FIELDS[step]];
    // `face` is sometimes a {status} object rather than a bare string.
    const value = String((raw && typeof raw === 'object' ? raw.status : raw) || '').toLowerCase();
    if (DONE_STATUSES.has(value)) return 'done';
    if (FAILED_STATUSES.has(value)) return 'failed';
    return 'pending';
};

type SortMode = 'uploaded' | 'name';

const WorkbenchTile: React.FC<{
    photo: Photo;
    thumb?: string;
    selected: boolean;
    onToggleSelect: () => void;
    infoOpen: boolean;
    onToggleInfo: () => void;
}> = ({ photo, thumb, selected, onToggleSelect, infoOpen, onToggleInfo }) => {
    const [meta, setMeta] = useState<PhotoMetadata | null | 'loading'>(null);

    const openInfo = () => {
        onToggleInfo();
        if (meta === null) {
            setMeta('loading');
            void fetchPhotoMetadata(photo.filename).then((res) => setMeta(res));
        }
    };

    return (
        <div className={`wb-tile mock-swatch ${photo.swatch}${selected ? ' selected' : ''}`}>
            <button type="button" className="wb-tile-select" aria-pressed={selected} aria-label={selected ? 'Deselect photo' : 'Select photo'} onClick={onToggleSelect}>
                <CheckCircleSolid />
            </button>
            <button type="button" className="wb-tile-info" aria-label="Photo details" onClick={(e) => { e.stopPropagation(); openInfo(); }}>
                <InformationCircleIcon />
            </button>
            {thumb && <img className="wb-tile-img" src={thumb} alt={photo.filename} loading="lazy" draggable={false} />}
            <div className="wb-tile-foot">
                <span className="wb-tile-name" title={photo.filename}>{photo.filename}</span>
                <div className="wb-tile-steps" aria-hidden="true">
                    {WORKBENCH_STEPS.map((step) => {
                        const status = statusFor(photo, step);
                        const Icon = status === 'failed' ? XCircleIcon : STEP_ICONS[step];
                        return <Icon key={step} className={`wb-step-icon ${status}`} title={`${step}: ${status}`} />;
                    })}
                </div>
            </div>
            {infoOpen && (
                <div className="wb-info-panel" onClick={(e) => e.stopPropagation()}>
                    <div className="wb-info-title">{photo.filename}</div>
                    {meta === 'loading' && <div className="wb-info-row muted">Loading…</div>}
                    {meta && meta !== 'loading' && (
                        <>
                            {meta.exifSummary?.camera && <div className="wb-info-row">{meta.exifSummary.camera}{meta.exifSummary.lens ? ` · ${meta.exifSummary.lens}` : ''}</div>}
                            {(meta.exifSummary?.fNumber || meta.exifSummary?.exposureTime || meta.exifSummary?.iso) && (
                                <div className="wb-info-row muted">
                                    {[meta.exifSummary?.fNumber, meta.exifSummary?.exposureTime, meta.exifSummary?.iso ? `ISO ${meta.exifSummary.iso}` : undefined].filter(Boolean).join(' · ')}
                                </div>
                            )}
                            {meta.resolution?.width && <div className="wb-info-row muted">{meta.resolution.width}×{meta.resolution.height}</div>}
                            {meta.location?.city && <div className="wb-info-row">{[meta.location.city, meta.location.country].filter(Boolean).join(', ')}</div>}
                            {(meta.tags?.length || meta.objects?.length) ? (
                                <div className="wb-info-tags">
                                    {[...(meta.tags ?? []), ...(meta.objects ?? [])].slice(0, 12).map((t) => <span key={t} className="wb-info-tag">{t}</span>)}
                                </div>
                            ) : null}
                            {!meta.exifSummary?.camera && !meta.location?.city && !meta.tags?.length && !meta.objects?.length && (
                                <div className="wb-info-row muted">No metadata yet.</div>
                            )}
                        </>
                    )}
                    {meta === null && <div className="wb-info-row muted">No metadata yet.</div>}
                </div>
            )}
        </div>
    );
};

export const WorkbenchGrid: React.FC<{
    photos: Photo[];
    selection: string[];
    onToggleSelect: (id: string) => void;
    onSelectMany: (ids: string[]) => void;
}> = ({ photos, selection, onToggleSelect, onSelectMany }) => {
    const [query, setQuery] = useState('');
    const [sort, setSort] = useState<SortMode>('uploaded');
    const [infoOpenId, setInfoOpenId] = useState<string | null>(null);

    const filtered = useMemo(() => {
        const q = query.trim().toLowerCase();
        const list = q ? photos.filter((p) => p.filename.toLowerCase().includes(q)) : photos.slice();
        list.sort((a, b) => {
            if (sort === 'name') return a.filename.localeCompare(b.filename);
            const ad = a.captureDate ? new Date(a.captureDate).getTime() : 0;
            const bd = b.captureDate ? new Date(b.captureDate).getTime() : 0;
            return bd - ad;
        });
        return list;
    }, [photos, query, sort]);

    const thumbs = usePhotoThumbnails(filtered);
    const selectedSet = new Set(selection);
    const allVisibleSelected = filtered.length > 0 && filtered.every((p) => selectedSet.has(p.id));

    return (
        <div className="wb-wrap">
            <div className="wb-toolbar">
                <div className="wb-search">
                    <MagnifyingGlassIcon />
                    <input
                        type="text"
                        placeholder="Search photos…"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                    />
                </div>
                <select className="wb-sort" value={sort} onChange={(e) => setSort(e.target.value as SortMode)}>
                    <option value="uploaded">Recently uploaded</option>
                    <option value="name">Filename</option>
                </select>
                <button
                    type="button"
                    className="pt-linkish"
                    onClick={() => onSelectMany(allVisibleSelected ? [] : filtered.map((p) => p.id))}
                >
                    {allVisibleSelected ? 'Deselect all' : `Select all (${filtered.length})`}
                </button>
            </div>

            {filtered.length === 0 ? (
                <p className="pt-grid-empty">No photos match “{query}”.</p>
            ) : (
                <div className="wb-grid">
                    {filtered.map((photo) => (
                        <WorkbenchTile
                            key={photo.id}
                            photo={photo}
                            thumb={thumbs[photo.filename]}
                            selected={selectedSet.has(photo.id)}
                            onToggleSelect={() => onToggleSelect(photo.id)}
                            infoOpen={infoOpenId === photo.id}
                            onToggleInfo={() => setInfoOpenId((cur) => (cur === photo.id ? null : photo.id))}
                        />
                    ))}
                </div>
            )}
        </div>
    );
};

export default WorkbenchGrid;
