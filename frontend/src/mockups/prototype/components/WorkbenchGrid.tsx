import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
    CircleCheck as CheckCircleSolid,
    Search as MagnifyingGlassIcon,
    Camera as CameraIcon,
    FileSearch as DocumentMagnifyingGlassIcon,
    FileText as DocumentTextIcon,
    Info as InformationCircleIcon,
    MapPin as MapPinIcon,
    Copy as Square2StackIcon,
    Sparkles as SparklesIcon,
    User as UserIcon,
} from 'lucide-react';
import { usePhotoThumbnails, fetchPhotoMetadata } from '../media';
import { ThumbSizeControl, useTileSize } from './controls';
import type { PhotoMetadata } from '../media';
import type { Photo } from '../types';
import { get, post } from '../../../services/apiClient';
import { mapPhoto } from '../store';
import ScrollSentinel from './ScrollSentinel';

// The Workbench tile carries a filename + 7 processing-step badges under the
// thumbnail, so it needs a larger floor than the plain photo grids.
const WB_TILE = { min: 150, max: 320, default: 200, step: 40 };

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

type StepStatus = 'done' | 'pending' | 'no-data' | 'failed';

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

// Mirrors getProcessingStatus's good/pending/warning/bad buckets from the
// (now-dead) legacy ToolsPage.tsx: done -> green, ran-but-found-nothing ->
// yellow, failed -> red, not-yet-run -> gray. Distinct from FAILED_STATUSES
// below -- 'no_data' means the step *ran successfully* and found nothing,
// not that it errored.
const DONE_STATUSES = new Set(['done']);
const NO_DATA_STATUSES = new Set(['no_data', 'skipped', 'unsupported']);
const FAILED_STATUSES = new Set(['failed', 'timeout']);

const statusFor = (photo: Photo, step: WorkbenchStep): StepStatus => {
    const raw = photo.processing?.[STEP_FIELDS[step]];
    // `face` is sometimes a {status} object rather than a bare string.
    const value = String((raw && typeof raw === 'object' ? raw.status : raw) || '').toLowerCase();
    if (DONE_STATUSES.has(value)) return 'done';
    if (NO_DATA_STATUSES.has(value)) return 'no-data';
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
    // Thumbnail URLs are attempted directly without the backend confirming the
    // blob exists first (see usePhotoThumbnails/media.ts), so a just-uploaded
    // photo's thumbnail can 404 while still generating. Hide on load failure
    // so the tile's swatch background shows through instead of a broken icon.
    const [thumbBroken, setThumbBroken] = useState(false);
    useEffect(() => setThumbBroken(false), [thumb]);

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
            {thumb && !thumbBroken && (
                <img
                    className="wb-tile-img"
                    src={thumb}
                    alt={photo.filename}
                    loading="lazy"
                    draggable={false}
                    onError={() => setThumbBroken(true)}
                />
            )}
            <div className="wb-tile-foot">
                <span className="wb-tile-name" title={photo.filename}>{photo.filename}</span>
                <div className="wb-tile-steps" aria-hidden="true">
                    {WORKBENCH_STEPS.map((step) => {
                        const status = statusFor(photo, step);
                        const Icon = STEP_ICONS[step];
                        // A small shape badge (✓ ✕ ! ·) rides each icon so status
                        // isn't conveyed by color alone (HIG/WCAG: don't rely on
                        // color to communicate meaning).
                        return (
                            <span key={step} className={`wb-step wb-step-${status}`} title={`${step}: ${status}`}>
                                <Icon className={`wb-step-icon ${status}`} />
                            </span>
                        );
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

const WB_PAGE = 120;
// Server-side "select all" walks ids in pages of 5000; stop at this many so a
// runaway click can't queue a million photos by accident.
const SELECT_ALL_CAP = 100000;

const stubPhoto = (filename: string): Photo => ({ id: filename, filename, swatch: 's1', dateLabel: '', year: 0, rating: 0, liked: false, placeId: null, personIds: [], tags: [] });

export const WorkbenchGrid: React.FC<{
    /** Deep-linked filenames: pinned at the top (while not searching) even if they are far down the library. */
    pinned?: string[];
    selection: string[];
    onToggleSelect: (id: string) => void;
    onSelectMany: (ids: string[]) => void;
}> = ({ pinned = [], selection, onToggleSelect, onSelectMany }) => {
    const [query, setQuery] = useState('');
    const [debounced, setDebounced] = useState('');
    const [sort, setSort] = useState<SortMode>('uploaded');
    const [infoOpenId, setInfoOpenId] = useState<string | null>(null);
    const [tile, setTile] = useTileSize('photostore.workbenchTileSize', WB_TILE);
    const [items, setItems] = useState<Photo[]>([]);
    const [total, setTotal] = useState<number | null>(null);
    const [hasMore, setHasMore] = useState(false);
    const [loading, setLoading] = useState(false);
    const [selectingAll, setSelectingAll] = useState(false);
    const [selectNote, setSelectNote] = useState('');
    const [pinnedPhotos, setPinnedPhotos] = useState<Photo[]>([]);
    const seq = useRef(0);
    const offsetRef = useRef(0);
    const loadingRef = useRef(false);

    useEffect(() => {
        const t = setTimeout(() => setDebounced(query.trim()), 250);
        return () => clearTimeout(t);
    }, [query]);

    const queryString = useCallback((extra: string) => {
        const sortParam = sort === 'name' ? 'name' : 'date';
        return `/photos?sort=${sortParam}${debounced ? `&nameContains=${encodeURIComponent(debounced)}` : ''}${extra}`;
    }, [sort, debounced]);

    const loadPage = useCallback(async (reset: boolean) => {
        if (loadingRef.current && !reset) return;
        const mine = ++seq.current;
        if (reset) offsetRef.current = 0;
        loadingRef.current = true;
        setLoading(true);
        try {
            const res = await get<{ photos?: Parameters<typeof mapPhoto>[0][]; total?: number }>(
                queryString(`&offset=${offsetRef.current}&limit=${WB_PAGE}&directMedia=1`),
            );
            if (mine !== seq.current) return;
            const page = Array.isArray(res?.photos) ? res.photos.map((p) => mapPhoto(p)) : [];
            offsetRef.current += page.length;
            setItems((prev) => (reset ? page : [...prev, ...page]));
            if (typeof res?.total === 'number') setTotal(res.total);
            setHasMore(page.length === WB_PAGE);
        } catch {
            if (mine === seq.current) setHasMore(false);
        } finally {
            if (mine === seq.current) {
                loadingRef.current = false;
                setLoading(false);
            }
        }
    }, [queryString]);

    useEffect(() => {
        loadingRef.current = false;
        void loadPage(true);
    }, [loadPage]);

    const pinnedKey = pinned.join('|');
    useEffect(() => {
        if (!pinned.length) { setPinnedPhotos([]); return undefined; }
        let active = true;
        setPinnedPhotos(pinned.map(stubPhoto));
        void post<{ photos?: Parameters<typeof mapPhoto>[0][] }>('/api/photos/lookup-batch', { filenames: pinned.slice(0, 200), directMedia: true })
            .then((res) => {
                if (!active || !Array.isArray(res?.photos)) return;
                const found = new Map(res.photos.map((p) => { const m = mapPhoto(p); return [m.filename, m] as const; }));
                setPinnedPhotos(pinned.map((f) => found.get(f) ?? stubPhoto(f)));
            })
            .catch(() => undefined);
        return () => { active = false; };
    }, [pinnedKey]); // eslint-disable-line react-hooks/exhaustive-deps

    const shown = React.useMemo(() => {
        if (debounced || !pinnedPhotos.length) return items;
        const pinnedNames = new Set(pinnedPhotos.map((p) => p.filename));
        return [...pinnedPhotos, ...items.filter((p) => !pinnedNames.has(p.filename))];
    }, [items, pinnedPhotos, debounced]);

    const thumbs = usePhotoThumbnails(shown);
    const selectedSet = new Set(selection);
    const allSelected = total !== null && total > 0 && selection.length >= Math.min(total, SELECT_ALL_CAP) && shown.every((p) => selectedSet.has(p.id));

    const selectAllMatching = async () => {
        if (allSelected) { onSelectMany([]); setSelectNote(''); return; }
        setSelectingAll(true);
        setSelectNote('');
        try {
            const names: string[] = [];
            for (;;) {
                const res = await get<{ filenames?: string[]; hasMore?: boolean }>(queryString(`&idsOnly=1&offset=${names.length}&limit=5000`));
                const batch = Array.isArray(res?.filenames) ? res.filenames : [];
                names.push(...batch);
                if (!res?.hasMore || batch.length === 0 || names.length >= SELECT_ALL_CAP) break;
            }
            const capped = names.slice(0, SELECT_ALL_CAP);
            onSelectMany(capped);
            if (total !== null && total > capped.length) setSelectNote(`Selected the first ${capped.length.toLocaleString()} of ${total.toLocaleString()} — narrow the search to cover the rest.`);
        } catch {
            setSelectNote('Couldn’t select everything — try again.');
        } finally {
            setSelectingAll(false);
        }
    };

    return (
        <div className="wb-wrap">
            <div className="wb-toolbar">
                <div className="wb-search">
                    <MagnifyingGlassIcon />
                    <input
                        type="text"
                        placeholder="Search filenames…"
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                    />
                </div>
                <select className="wb-sort" value={sort} onChange={(e) => setSort(e.target.value as SortMode)}>
                    <option value="uploaded">Recently uploaded</option>
                    <option value="name">Filename</option>
                </select>
                <ThumbSizeControl value={tile} onChange={setTile} min={WB_TILE.min} max={WB_TILE.max} step={WB_TILE.step} />
                <button type="button" className="pt-linkish" onClick={() => void selectAllMatching()} disabled={selectingAll || total === 0}>
                    {selectingAll ? 'Selecting…' : allSelected ? 'Deselect all' : `Select all${total !== null ? ` (${total.toLocaleString()})` : ''}`}
                </button>
            </div>
            {selectNote && <p className="pt-menu-label">{selectNote}</p>}

            {shown.length === 0 && !loading ? (
                <p className="pt-grid-empty">{debounced ? `No photos match “${debounced}”.` : 'No photos yet.'}</p>
            ) : (
                <div className="wb-grid" style={{ ['--wb-tile-min' as string]: `${tile}px` } as React.CSSProperties}>
                    {shown.map((photo) => (
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
            {hasMore && <ScrollSentinel onVisible={() => void loadPage(false)} deps={items.length} />}
        </div>
    );
};

export default WorkbenchGrid;
