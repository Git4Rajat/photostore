import React, { useEffect, useMemo, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
    ArrowPathIcon,
    ClockIcon,
    BoltIcon,
    CameraIcon,
    EyeIcon,
    ExclamationTriangleIcon,
    InformationCircleIcon,
    DocumentTextIcon,
    MagnifyingGlassIcon,
    MapIcon,
    PhotoIcon,
    UserCircleIcon,
    UsersIcon,
} from '@heroicons/react/24/outline';
import { get, post } from '../services/apiClient';
import { useAppServices } from './AppServicesProvider';
import type { BrowserProcessingAction } from './AppServicesProvider';
import PhotoTile from './shared/PhotoTile';
import PhotoViewer from './shared/PhotoViewer';

interface Photo {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    uploadDate?: string;
    processing?: {
        thumbnail?: string;
        exif?: string;
        ocr?: string;
        aiVision?: string;
        mapDetection?: string;
        face?: string | { status?: string; source?: string; detectionSource?: string };
        faceSource?: string;
    };
    rating?: number;
    likes?: number;
    liked?: boolean;
    tags?: string[];
    rotation?: number;
    thumbnailRotation?: number;
    location?: {
        latitude?: string;
        longitude?: string;
        address?: string;
        city?: string;
        country?: string;
    };
    exifSummary?: {
        capturedAt?: string;
        camera?: string;
        lens?: string;
    };
}

interface ToolPhotoMetadata {
    faceCount?: number;
    tags?: string[];
    location?: {
        latitude?: string;
        longitude?: string;
        address?: string;
        city?: string;
        country?: string;
    };
    exifSummary?: {
        capturedAt?: string;
        camera?: string;
        lens?: string;
    };
    resolution?: {
        width?: number;
        height?: number;
    };
}

type ToolAction = BrowserProcessingAction | 'peopleIndex' | 'vectorIndex';
type ProcessingFilterState = 'all' | 'failed' | 'no_data';
type ProcessingFilterProcess = 'all' | 'thumbnail' | 'exif' | 'ocr' | 'aiVision' | 'mapDetection' | 'face';
type ChipStepKey = 'thumbnail' | 'exif' | 'ocr' | 'aiVision' | 'mapDetection' | 'face';
type QueueStageKey = keyof QueueStatus;
type WorkbenchViewKey = 'recent' | 'all' | 'attention';

type QueueStageConfig = {
    key: QueueStageKey;
    label: string;
    description: string;
    icon: React.ComponentType<React.SVGProps<SVGSVGElement>>;
};

type QueueStatus = {
    thumbnail?: { queued?: number; pending?: number; pendingTotal?: number; running?: number; failed?: number; noData?: number };
    exif?: { queued?: number; pending?: number; pendingTotal?: number; running?: number; failed?: number; noData?: number };
    ocr?: { queued?: number; pending?: number; pendingTotal?: number; running?: number; failed?: number; noData?: number };
    ai_vision?: { queued?: number; pending?: number; pendingTotal?: number; running?: number; failed?: number; noData?: number };
    map_detection?: { queued?: number; pending?: number; pendingTotal?: number; running?: number; failed?: number; noData?: number };
    face?: { queued?: number; pending?: number; pendingTotal?: number; running?: number; failed?: number; noData?: number };
};

type QueueStatusResponse = QueueStatus & { generatedAt?: string };
type ToolsPageKey = 'overview' | 'queue-status' | 'browser-workbench' | 'recovery';

const processingStateLabels: Record<ProcessingFilterState, string> = {
    all: 'All',
    failed: 'Failed',
    no_data: 'No data',
};

const processingProcessLabels: Record<ProcessingFilterProcess, string> = {
    all: 'All processes',
    thumbnail: 'Thumbnail',
    exif: 'EXIF',
    ocr: 'OCR',
    aiVision: 'Vision',
    mapDetection: 'Map',
    face: 'Face',
};

const processingServiceLabels: Record<ChipStepKey, string> = {
    thumbnail: 'Thumbnail',
    exif: 'EXIF',
    ocr: 'OCR',
    aiVision: 'AI vision',
    mapDetection: 'Map tagging',
    face: 'Faces',
};

const workbenchViewLabels: Record<WorkbenchViewKey, string> = {
    recent: 'Recent uploads',
    all: 'All photos',
    attention: 'Attention',
};

const processingStageIcons: Record<ChipStepKey, React.ComponentType<React.SVGProps<SVGSVGElement>>> = {
    thumbnail: PhotoIcon,
    exif: InformationCircleIcon,
    ocr: DocumentTextIcon,
    aiVision: EyeIcon,
    mapDetection: MapIcon,
    face: UserCircleIcon,
};

const queueStageCards: QueueStageConfig[] = [
    { key: 'thumbnail', label: 'Thumbnails', description: 'Browser-created previews', icon: processingStageIcons.thumbnail },
    { key: 'exif', label: 'EXIF', description: 'Capture and GPS metadata', icon: processingStageIcons.exif },
    { key: 'ocr', label: 'OCR', description: 'Browser text extraction', icon: processingStageIcons.ocr },
    { key: 'ai_vision', label: 'AI vision', description: 'Browser tags and captions', icon: processingStageIcons.aiVision },
    { key: 'map_detection', label: 'Map tagging', description: 'Browser reverse geocode', icon: processingStageIcons.mapDetection },
    { key: 'face', label: 'Face detection', description: 'Browser detection + clustering', icon: processingStageIcons.face },
];

const getProcessingStatus = (photo: Photo, step: ChipStepKey) => {
    const value = photo.processing?.[step as keyof NonNullable<Photo['processing']>];
    if (step === 'face' && value && typeof value === 'object') {
        return String((value as { status?: string }).status || 'unqueued').toLowerCase();
    }
    return String(value || 'unqueued').toLowerCase();
};

const getStatusTone = (status: string) => {
    if (status === 'done') return 'good';
    if (status === 'queued' || status === 'pending' || status === 'running') return 'pending';
    if (status === 'no_data') return 'warning';
    if (status === 'failed') return 'bad';
    return 'unknown';
};

const hasProcessingAttention = (photo: Photo) => (
    (Object.keys(processingServiceLabels) as ChipStepKey[]).some((step) => {
        const status = getProcessingStatus(photo, step);
        return status === 'failed' || status === 'no_data';
    })
);

const PhotoProcessingChip = ({ label, status, step }: { label: string; status: string; step: ChipStepKey }) => {
    const normalized = String(status || 'unqueued').toLowerCase();
    const tone = getStatusTone(normalized);
    const StatusIcon = processingStageIcons[step];
    return (
        <span className={`tools-photo-status-chip tone-${tone} status-${normalized.replace(/[^a-z0-9_-]/g, '-')}`} title={`${label}: ${normalized.replace('_', ' ')}`} aria-label={`${label}: ${normalized.replace('_', ' ')}`}>
            <span className="tools-status-indicator" aria-hidden="true">
                <StatusIcon className="tools-status-indicator-icon" />
            </span>
            <span className="sr-only">{label}: {normalized.replace('_', ' ')}</span>
        </span>
    );
};

const getToolsPageKey = (pathname: string): ToolsPageKey => {
    if (pathname.startsWith('/tools/queue-status')) {
        return 'queue-status';
    }
    if (pathname.startsWith('/tools/browser-workbench')) {
        return 'browser-workbench';
    }
    if (pathname.startsWith('/tools/recovery')) {
        return 'recovery';
    }
    return 'overview';
};

const PAGE_SIZE = 50;

const ToolsPage: React.FC = () => {
    const { browserAiModelState, loadBrowserAiModel, startBrowserProcessing } = useAppServices();
    const location = useLocation();
    const [photos, setPhotos] = useState<Photo[]>([]);
    const [photosTotal, setPhotosTotal] = useState<number>(0);
    const [photosOffset, setPhotosOffset] = useState<number>(0);
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [loading, setLoading] = useState<boolean>(false);
    const [loadingMore, setLoadingMore] = useState<boolean>(false);
    const [running, setRunning] = useState<ToolAction | null>(null);
    const [message, setMessage] = useState<string>('');
    const [queueLoadWarning, setQueueLoadWarning] = useState<string>('');
    const [expandedInfo, setExpandedInfo] = useState<Set<string>>(new Set());
    const [infoByFile, setInfoByFile] = useState<Record<string, ToolPhotoMetadata>>({});
    const [loadingInfo, setLoadingInfo] = useState<Set<string>>(new Set());
    const [queueStatus, setQueueStatus] = useState<QueueStatus>({});
    const [isQueueStatusExpanded, setIsQueueStatusExpanded] = useState<boolean>(true);
    const [forceRun, setForceRun] = useState<boolean>(false);
    const [runAll, setRunAll] = useState<boolean>(false);
    const [processingFilterState, setProcessingFilterState] = useState<ProcessingFilterState>('all');
    const [processingFilterProcess, setProcessingFilterProcess] = useState<ProcessingFilterProcess>('all');
    const [viewMode, setViewMode] = useState<WorkbenchViewKey>('recent');
    const [viewerIndex, setViewerIndex] = useState<number | null>(null);
    const activeToolsPage = getToolsPageKey(location.pathname);
    const isOverviewPage = activeToolsPage === 'overview';
    const isQueueStatusPage = activeToolsPage === 'queue-status';
    const isBrowserWorkbenchPage = activeToolsPage === 'browser-workbench';
    const isRecoveryPage = activeToolsPage === 'recovery';

    const loadPhotos = async (queryText: string = '') => {
        setLoading(true);
        setMessage('');
        try {
            const trimmedQuery = queryText.trim();
            const response = trimmedQuery
                ? await get(`/photos/search?q=${encodeURIComponent(trimmedQuery)}&offset=0&limit=${PAGE_SIZE}`)
                : await get(`/photos?offset=0&limit=${PAGE_SIZE}`);
            const fetched = Array.isArray(response?.photos) ? response.photos : [];
            setPhotos(fetched);
            setPhotosTotal(Number(response?.total ?? fetched.length));
            setPhotosOffset(fetched.length);
        } catch (err) {
            setMessage(`Failed to load photos: ${String(err)}`);
        } finally {
            setLoading(false);
        }
    };

    const loadMorePhotos = async () => {
        if (loadingMore || loading) return;
        setLoadingMore(true);
        try {
            const response = await get(`/photos?offset=${photosOffset}&limit=${PAGE_SIZE}`);
            const fetched = Array.isArray(response?.photos) ? response.photos : [];
            setPhotos((prev) => [...prev, ...fetched]);
            setPhotosTotal(Number(response?.total ?? (photosOffset + fetched.length)));
            setPhotosOffset((prev) => prev + fetched.length);
        } catch (err) {
            setMessage(`Failed to load more photos: ${String(err)}`);
        } finally {
            setLoadingMore(false);
        }
    };

    const hasMorePhotos = photos.length < photosTotal;

    const loadQueueStatus = async () => {
        try {
            const response = (await get(`/upload/processing/status?ts=${Date.now()}`)) as QueueStatusResponse;
            setQueueStatus({
                thumbnail: response?.thumbnail,
                exif: response?.exif,
                ocr: response?.ocr,
                ai_vision: response?.ai_vision,
                map_detection: response?.map_detection,
                face: response?.face,
            });
            setQueueLoadWarning('');
        } catch (err) {
            setQueueLoadWarning(`Queue status unavailable: ${String(err)}`);
        }
    };

    useEffect(() => {
        void loadQueueStatus();
        if (isOverviewPage || isBrowserWorkbenchPage) {
            void loadPhotos();
        }
    }, [activeToolsPage]);

    useEffect(() => {
        const mediaQuery = window.matchMedia('(max-width: 600px)');
        const updateQueueStatusExpanded = () => {
            setIsQueueStatusExpanded(!mediaQuery.matches);
        };
        updateQueueStatusExpanded();
        mediaQuery.addEventListener('change', updateQueueStatusExpanded);
        return () => mediaQuery.removeEventListener('change', updateQueueStatusExpanded);
    }, []);

    useEffect(() => {
        const timer = window.setInterval(() => {
            void loadQueueStatus();
        }, 30000);
        return () => window.clearInterval(timer);
    }, []);

    const filtered = useMemo(() => {
        return photos.filter((photo) => {
            const processMatch = processingFilterProcess === 'all'
                ? true
                : getProcessingStatus(photo, processingFilterProcess) !== 'unqueued';
            const stateMatch = processingFilterState === 'all'
                ? true
                : (processingFilterProcess === 'all'
                    ? (Object.keys(processingServiceLabels) as ChipStepKey[]).some((step) => getProcessingStatus(photo, step) === processingFilterState)
                    : getProcessingStatus(photo, processingFilterProcess) === processingFilterState);
            return processMatch && stateMatch;
        });
    }, [photos, processingFilterProcess, processingFilterState]);

    const workbenchPhotos = useMemo(() => {
        let items = [...filtered];
        if (viewMode === 'attention') {
            items = items.filter((photo) => hasProcessingAttention(photo));
        }
        if (viewMode === 'recent') {
            items.sort((a, b) => {
                const left = a.uploadDate ? Date.parse(a.uploadDate) : 0;
                const right = b.uploadDate ? Date.parse(b.uploadDate) : 0;
                return right - left;
            });
            return items.slice(0, 20);
        }
        return items;
    }, [filtered, viewMode]);
    const previewPhotos = useMemo(() => {
        const items = [...filtered];
        items.sort((a, b) => {
            const left = a.uploadDate ? Date.parse(a.uploadDate) : 0;
            const right = b.uploadDate ? Date.parse(b.uploadDate) : 0;
            return right - left;
        });
        return items.slice(0, 12);
    }, [filtered]);

    const overviewPhotos = useMemo(() => {
        return viewMode === 'recent' ? previewPhotos : workbenchPhotos;
    }, [viewMode, previewPhotos, workbenchPhotos]);

    const selectedVisibleCount = useMemo(
        () => workbenchPhotos.filter((photo) => selected.has(photo.filename)).length,
        [selected, workbenchPhotos],
    );
    const selectedOutsideViewCount = Math.max(0, selected.size - selectedVisibleCount);

    const toggleOne = (filename: string) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(filename)) next.delete(filename);
            else next.add(filename);
            return next;
        });
    };

    const clearSelection = () => setSelected(new Set());
    const selectAllPreview = () => setSelected((prev) => {
        const next = new Set(prev);
        overviewPhotos.forEach((photo) => next.add(photo.filename));
        return next;
    });

    const toggleInfo = async (filename: string) => {
        setExpandedInfo((prev) => {
            const next = new Set(prev);
            if (next.has(filename)) next.delete(filename);
            else next.add(filename);
            return next;
        });
        if (infoByFile[filename]) {
            return;
        }
        setLoadingInfo((prev) => new Set(prev).add(filename));
        try {
            const response = await get(`/photos/${encodeURIComponent(filename)}/metadata`);
            setInfoByFile((prev) => ({ ...prev, [filename]: response as ToolPhotoMetadata }));
        } catch (err) {
            setMessage(`Failed to load info for ${filename}: ${String(err)}`);
        } finally {
            setLoadingInfo((prev) => {
                const next = new Set(prev);
                next.delete(filename);
                return next;
            });
        }
    };

    const buildMapLabel = (location?: ToolPhotoMetadata['location']) => {
        if (!location) return '';
        // "City, Country" is the primary format -- town/city and country are
        // what people recognize a place by, not the full street address.
        const cityCountry = [location.city, location.country].filter(Boolean);
        if (cityCountry.length > 0) return cityCountry.join(', ');
        if (location.address) return location.address;
        if (location.latitude || location.longitude) return `${location.latitude || ''} ${location.longitude || ''}`.trim();
        return '';
    };

    const formatCapturedAt = (value?: string) => {
        if (!value) return '';
        // EXIF datetimes use "YYYY:MM:DD HH:MM:SS" for the date portion.
        const normalized = value.replace(/^(\d{4}):(\d{2}):(\d{2})/, '$1-$2-$3');
        const parsed = new Date(normalized);
        if (Number.isNaN(parsed.getTime())) {
            return value;
        }
        return parsed.toLocaleString(undefined, {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit',
        });
    };

    const formatResolution = (resolution?: ToolPhotoMetadata['resolution']) => {
        const width = Number(resolution?.width || 0);
        const height = Number(resolution?.height || 0);
        if (!width || !height) return '';
        const megapixels = (width * height) / 1_000_000;
        return `${width} x ${height} (${megapixels.toFixed(1)} MP)`;
    };

    const renderPhotoInfo = (photo: Photo) => {
        if (!expandedInfo.has(photo.filename)) {
            return null;
        }
        const info = infoByFile[photo.filename];
        const tags = info?.tags || photo.tags || [];
        const locationLabel = buildMapLabel(info?.location || photo.location);
        const exifSummary = info?.exifSummary || photo.exifSummary;
        const capturedAtLabel = formatCapturedAt(exifSummary?.capturedAt);
        const resolutionLabel = formatResolution(info?.resolution);
        const faceCount = Number(info?.faceCount || 0);
        const hasAnyInfo = tags.length > 0
            || Boolean(locationLabel)
            || faceCount > 0
            || Boolean(capturedAtLabel)
            || Boolean(resolutionLabel)
            || Boolean(exifSummary?.camera)
            || Boolean(exifSummary?.lens);
        return (
            <div className="tools-photo-info" onClick={(event) => event.stopPropagation()}>
                {loadingInfo.has(photo.filename) && <span className="tools-info-muted">Loading...</span>}
                {!loadingInfo.has(photo.filename) && (
                    <>
                        {tags.length > 0 && (
                            <div className="tools-info-tags" aria-label={`Vision tags for ${photo.filename}`}>
                                {tags.slice(0, 12).map((tag) => (
                                    <span key={tag} className="tools-info-tag">{tag}</span>
                                ))}
                            </div>
                        )}
                        {locationLabel && (
                            <div className="tools-info-location">
                                <MapIcon className="tools-info-icon" aria-hidden="true" />
                                <span>{locationLabel}</span>
                            </div>
                        )}
                        {faceCount > 0 && (
                            <div className="tools-info-location">
                                <UserCircleIcon className="tools-info-icon" aria-hidden="true" />
                                <span>{faceCount} face{faceCount === 1 ? '' : 's'}</span>
                            </div>
                        )}
                        {capturedAtLabel && (
                            <div className="tools-info-location">
                                <ClockIcon className="tools-info-icon" aria-hidden="true" />
                                <span>{capturedAtLabel}</span>
                            </div>
                        )}
                        {resolutionLabel && (
                            <div className="tools-info-location">
                                <PhotoIcon className="tools-info-icon" aria-hidden="true" />
                                <span>{resolutionLabel}</span>
                            </div>
                        )}
                        {(exifSummary?.camera || exifSummary?.lens) && (
                            <div className="tools-info-location">
                                <CameraIcon className="tools-info-icon" aria-hidden="true" />
                                <span>{[exifSummary?.camera, exifSummary?.lens].filter(Boolean).join(' - ')}</span>
                            </div>
                        )}
                        {!hasAnyInfo && (
                            <span className="tools-info-muted">No tags or location yet.</span>
                        )}
                    </>
                )}
            </div>
        );
    };

    const runBrowserAction = async (action: BrowserProcessingAction) => {
        const actionPhotos = isBrowserWorkbenchPage ? workbenchPhotos : previewPhotos;
        const useAllMatching = isBrowserWorkbenchPage && runAll;
        const selectedPhotos = useAllMatching
            ? actionPhotos
            : actionPhotos.filter((photo) => selected.has(photo.filename));
        if (selectedPhotos.length === 0) {
            setMessage(useAllMatching
                ? 'No photos match the current filters.'
                : (isBrowserWorkbenchPage
                    ? 'Select photos or switch the scope to all filtered photos before starting browser processing.'
                    : 'Select photos in Preview before starting browser processing.'));
            return;
        }
        const filenames = selectedPhotos.map((photo) => photo.filename);
        const items = selectedPhotos.map((photo) => ({
            filename: photo.filename,
            rotation: photo.rotation,
        }));
        setRunning(action);
        setMessage('');
        try {
            if (action === 'vision' && browserAiModelState.status !== 'available') {
                const modelState = await loadBrowserAiModel();
                if (modelState.status !== 'available') {
                    setMessage(`Browser AI is not available: ${modelState.detail || modelState.reason || modelState.status}.`);
                    return;
                }
            }
            setMessage(`Starting browser processing for ${filenames.length} photo(s)...`);
            const processed = await startBrowserProcessing({
                actions: [action],
                filenames,
                items,
                force: forceRun,
            });
            setMessage(`Browser processing finished. processed=${processed}, requested=${filenames.length}`);
            await loadPhotos();
            await loadQueueStatus();
        } catch (err) {
            setMessage(`Browser '${action}' failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const runReclusterPeople = async () => {
        if (!window.confirm('Run protected people recluster repair? Current assignments will be snapshotted first, but this should only be used for admin recovery.')) {
            return;
        }
        setRunning('peopleIndex');
        setMessage('Preparing protected people repair...');
        try {
            const response = await post('/api/admin/people/recluster', {
                queue: true,
                allowReassignConfirmed: false,
                confirm: 'RECLUSTER_REPAIR',
                repair: true,
            });
            if (response?.queued || response?.status === 'queued' || response?.status === 'already_queued') {
                const jobId = String(response?.jobId || 'pending');
                const status = response?.status === 'already_queued' ? 'already queued' : 'queued';
                setMessage(`People recluster ${status}. jobId=${jobId}`);
            } else {
                setMessage(`People recluster finished. processed=${Number(response?.processed || 0)}, failed=${Number(response?.failed || 0)}, people=${Number(response?.peopleCount || 0)}, faces=${Number(response?.faceCount || 0)}`);
            }
            await loadQueueStatus();
        } catch (err) {
            setMessage(`People recluster failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const runRebuildVectorIndex = async () => {
        if (!window.confirm('Rebuild the cached vector index for this account? This refreshes the on-disk .npz file from the latest face embeddings.')) {
            return;
        }
        setRunning('vectorIndex');
        setMessage('Rebuilding vector index...');
        try {
            const response = await post('/api/admin/vector-index/rebuild', {
                confirm: 'REBUILD_VECTOR_INDEX',
                repair: true,
            });
            const rowCount = Number(response?.rowCount || 0);
            if (response?.status === 'empty' || rowCount === 0) {
                setMessage('Vector index rebuild finished, but no embeddings were available to index.');
            } else {
                const updatedAt = response?.updatedAt ? ` updatedAt=${response.updatedAt}` : '';
                setMessage(`Vector index rebuilt. rowCount=${rowCount}${updatedAt}`);
            }
            await loadQueueStatus();
        } catch (err) {
            setMessage(`Vector index rebuild failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const renderProcessingFilters = () => (
        <div className="tools-filter-row" aria-label="Processing filters">
            <select
                className="field field-select tools-filter-select"
                aria-label="Processing state filter"
                value={processingFilterState}
                onChange={(event) => setProcessingFilterState(event.target.value as ProcessingFilterState)}
            >
                {(Object.keys(processingStateLabels) as ProcessingFilterState[]).map((state) => (
                    <option key={state} value={state}>
                        {processingStateLabels[state]}
                    </option>
                ))}
            </select>
            <select
                className="field field-select tools-filter-select"
                aria-label="Processing process filter"
                value={processingFilterProcess}
                onChange={(event) => setProcessingFilterProcess(event.target.value as ProcessingFilterProcess)}
            >
                {(Object.keys(processingProcessLabels) as ProcessingFilterProcess[]).map((process) => (
                    <option key={process} value={process}>
                        {processingProcessLabels[process]}
                    </option>
                ))}
            </select>
        </div>
    );

    const renderWorkbenchViewToggle = () => (
        <div className="tools-view-toggle" aria-label="Gallery view">
            {(Object.keys(workbenchViewLabels) as WorkbenchViewKey[]).map((mode) => (
                <button
                    key={mode}
                    type="button"
                    className={`btn btn-soft ${viewMode === mode ? 'active' : ''}`}
                    onClick={() => setViewMode(mode)}
                    aria-pressed={viewMode === mode}
                >
                    {workbenchViewLabels[mode]}
                </button>
            ))}
        </div>
    );

    const renderOverviewPage = () => (
        <>
            <details
                className="tools-panel tools-queue tools-queue-toggle"
                open={isQueueStatusExpanded}
                onToggle={(event) => setIsQueueStatusExpanded((event.currentTarget as HTMLDetailsElement).open)}
            >
                <summary className="tools-panel-header tools-queue-summary">
                    <div>
                        <h2 className="tools-panel-title">Queue status</h2>
                    </div>
                    <button type="button" className="btn btn-soft icon-btn" onClick={(event) => { event.preventDefault(); void loadQueueStatus(); }} disabled={!!running} aria-label="Refresh queue status" title="Refresh queue status">
                        <ArrowPathIcon className="toolbar-icon" />
                        <span className="sr-only">Refresh queue status</span>
                    </button>
                </summary>
                <div className="tools-queue-grid">
                    {queueStageCards.map((item) => {
                        const summary = queueStatus[item.key] || {};
                        const waitingTotal = Number(summary.pendingTotal ?? 0);
                        return (
                            <div key={item.key} className="tools-queue-card">
                                <div className="tools-queue-card-top">
                                    <span className="tools-queue-icon" aria-hidden="true">
                                        <item.icon className="tools-queue-icon-svg" />
                                    </span>
                                </div>
                                <div className="tools-queue-counts">
                                    <span className="tools-queue-chip" aria-label={`Remaining ${waitingTotal}`} title={`Remaining ${waitingTotal}`}>
                                        <ClockIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                        <span className="tools-queue-chip-value">{waitingTotal}</span>
                                    </span>
                                    <span className="tools-queue-chip" aria-label={`Running ${summary.running ?? 0}`} title={`Running ${summary.running ?? 0}`}>
                                        <ArrowPathIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                        <span className="tools-queue-chip-value">{summary.running ?? 0}</span>
                                    </span>
                                    <span className="tools-queue-chip" aria-label={`No data ${summary.noData ?? 0}`} title={`No data ${summary.noData ?? 0}`}>
                                        <InformationCircleIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                        <span className="tools-queue-chip-value">{summary.noData ?? 0}</span>
                                    </span>
                                    <span className="tools-queue-chip" aria-label={`Failed ${summary.failed ?? 0}`} title={`Failed ${summary.failed ?? 0}`}>
                                        <ExclamationTriangleIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                        <span className="tools-queue-chip-value">{summary.failed ?? 0}</span>
                                    </span>
                                </div>
                            </div>
                        );
                    })}
                </div>
            </details>

            <div className="tools-panel tools-actions-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Actions</h2>
                    </div>
                </div>
                <div className="tools-action-grid tools-icon-action-grid" aria-label="Tools actions">
                    <button
                        type="button"
                        className={`btn btn-soft icon-btn tools-action-button${forceRun ? ' active' : ''}`}
                        onClick={() => setForceRun((value) => !value)}
                        aria-label="Force for all"
                        aria-pressed={forceRun}
                        title={forceRun ? 'Force for all on' : 'Force for all off'}
                    >
                        <BoltIcon className="toolbar-icon" />
                        <span className="sr-only">{forceRun ? 'Force for all on' : 'Force for all off'}</span>
                    </button>
                    <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runBrowserAction('thumbnails')} aria-label="Run thumbnails" title="Run thumbnails">
                        <PhotoIcon className="toolbar-icon" />
                        <span className="sr-only">Run thumbnails</span>
                    </button>
                    <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runBrowserAction('ocr')} aria-label="Run OCR" title="Run OCR">
                        <DocumentTextIcon className="toolbar-icon" />
                        <span className="sr-only">Run OCR</span>
                    </button>
                    <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runBrowserAction('vision')} aria-label="Run vision" title="Run vision">
                        <EyeIcon className="toolbar-icon" />
                        <span className="sr-only">Run vision</span>
                    </button>
                    <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runBrowserAction('exif')} aria-label="Run exif" title="Run exif">
                        <InformationCircleIcon className="toolbar-icon" />
                        <span className="sr-only">Run exif</span>
                    </button>
                    <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runBrowserAction('map')} aria-label="Run geo" title="Run geo">
                        <MapIcon className="toolbar-icon" />
                        <span className="sr-only">Run geo</span>
                    </button>
                    <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runBrowserAction('faces')} aria-label="Run face" title="Run face">
                        <UserCircleIcon className="toolbar-icon" />
                        <span className="sr-only">Run face</span>
                    </button>
                <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runRebuildVectorIndex()} aria-label="Run vector rebuild" title="Run vector rebuild">
                        <MagnifyingGlassIcon className="toolbar-icon" />
                        <span className="sr-only">Run vector rebuild</span>
                    </button>
                    <button type="button" className="btn btn-soft icon-btn tools-action-button" onClick={() => void runReclusterPeople()} aria-label="Run reclustering" title="Run reclustering">
                        <UsersIcon className="toolbar-icon" />
                        <span className="sr-only">Run reclustering</span>
                    </button>
                </div>
            </div>

            <div className="tools-panel tools-filter-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Filters</h2>
                    </div>
                </div>
                {renderProcessingFilters()}
            </div>

            <div className="tools-panel tools-gallery-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Preview</h2>
                    </div>
                    {renderWorkbenchViewToggle()}
                </div>
                {queueLoadWarning && <p className="status">{queueLoadWarning}</p>}
                {loading && <p className="status">Loading photos...</p>}
                {!loading && photos.length === 0 && <p className="empty">No photos found.</p>}
                {!loading && photos.length > 0 && overviewPhotos.length === 0 && <p className="empty">No photos match the current filters.</p>}
                <div className="tools-selection-actions">
                    <button
                        type="button"
                        className="btn btn-soft"
                        onClick={selectAllPreview}
                        disabled={overviewPhotos.length === 0}
                        aria-label={`Select all ${workbenchViewLabels[viewMode].toLowerCase()}`}
                    >
                        Select all
                    </button>
                    <button type="button" className="btn btn-soft" onClick={clearSelection} disabled={selected.size === 0}>
                        Clear selection
                    </button>
                    <span className="tools-panel-meta">Selected photos: {selected.size}</span>
                </div>
                <div className="tools-panel-header tools-section-header">
                    <h3 className="tools-panel-title">{workbenchViewLabels[viewMode]}</h3>
                    <span className="tools-panel-meta">{overviewPhotos.length} shown</span>
                </div>
                <div className="gallery-grid">
                    {overviewPhotos.map((photo, index) => (
                        <PhotoTile
                            key={photo.filename}
                            photo={photo}
                            title={photo.filename}
                            selected={selected.has(photo.filename)}
                            openOriginal={false}
                            selectableOverlay={(
                                <input
                                    type="checkbox"
                                    aria-label={`Toggle ${photo.filename}`}
                                    checked={selected.has(photo.filename)}
                                    onClick={(event) => event.stopPropagation()}
                                    onChange={() => toggleOne(photo.filename)}
                                />
                            )}
                            onCardClick={() => setViewerIndex(index)}
                            bodyContent={(
                                <>
                                    <div className="tools-photo-status-row" aria-label={`Processing status for ${photo.filename}`}>
                                        <PhotoProcessingChip label="Thumbnail" status={getProcessingStatus(photo, 'thumbnail')} step="thumbnail" />
                                        <PhotoProcessingChip label="EXIF" status={getProcessingStatus(photo, 'exif')} step="exif" />
                                        <PhotoProcessingChip label="OCR" status={getProcessingStatus(photo, 'ocr')} step="ocr" />
                                        <PhotoProcessingChip label="VISION" status={getProcessingStatus(photo, 'aiVision')} step="aiVision" />
                                        <PhotoProcessingChip label="MAP" status={getProcessingStatus(photo, 'mapDetection')} step="mapDetection" />
                                        <PhotoProcessingChip label="FACE" status={getProcessingStatus(photo, 'face')} step="face" />
                                        <button type="button" className="btn btn-soft icon-btn" onClick={(event) => { event.stopPropagation(); void toggleInfo(photo.filename); }} aria-label={`Toggle info for ${photo.filename}`} title={`Toggle info for ${photo.filename}`}>
                                            <InformationCircleIcon className="toolbar-icon" />
                                            <span className="sr-only">Toggle info for {photo.filename}</span>
                                        </button>
                                    </div>
                                    {renderPhotoInfo(photo)}
                                </>
                            )}
                        />
                    ))}
                </div>
                {hasMorePhotos && viewMode !== 'recent' && (
                    <div className="tools-load-more">
                        <button
                            type="button"
                            className="btn btn-soft"
                            onClick={() => void loadMorePhotos()}
                            disabled={loadingMore}
                        >
                            {loadingMore ? 'Loading…' : `Load more (${photos.length} of ${photosTotal})`}
                        </button>
                    </div>
                )}
                {viewerIndex !== null && overviewPhotos[viewerIndex] && (
                    <PhotoViewer
                        photos={overviewPhotos}
                        index={viewerIndex}
                        onClose={() => setViewerIndex(null)}
                        onIndexChange={(index: number) => setViewerIndex(index)}
                    />
                )}
            </div>
        </>
    );

    const renderQueueStatusPage = () => (
        <div className="tools-panel tools-queue">
            <div className="tools-panel-header">
                <div>
                    <h2 className="tools-panel-title">Queue status</h2>
                </div>
            </div>
            <div className="tools-queue-grid">
                {queueStageCards.map((item) => {
                    const summary = queueStatus[item.key] || {};
                    const waitingTotal = Number(summary.pendingTotal ?? 0);
                    return (
                        <div key={item.key} className="tools-queue-card">
                            <div className="tools-queue-card-top">
                                <span className="tools-queue-icon" aria-hidden="true">
                                    <item.icon className="tools-queue-icon-svg" />
                                </span>
                            </div>
                            <div className="tools-queue-counts">
                                <span className="tools-queue-chip" aria-label={`Remaining ${waitingTotal}`} title={`Remaining ${waitingTotal}`}>
                                    <ClockIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                    <span className="tools-queue-chip-value">{waitingTotal}</span>
                                </span>
                                <span className="tools-queue-chip" aria-label={`Running ${summary.running ?? 0}`} title={`Running ${summary.running ?? 0}`}>
                                    <ArrowPathIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                    <span className="tools-queue-chip-value">{summary.running ?? 0}</span>
                                </span>
                                <span className="tools-queue-chip" aria-label={`No data ${summary.noData ?? 0}`} title={`No data ${summary.noData ?? 0}`}>
                                    <InformationCircleIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                    <span className="tools-queue-chip-value">{summary.noData ?? 0}</span>
                                </span>
                                <span className="tools-queue-chip" aria-label={`Failed ${summary.failed ?? 0}`} title={`Failed ${summary.failed ?? 0}`}>
                                    <ExclamationTriangleIcon className="tools-queue-chip-icon" aria-hidden="true" />
                                    <span className="tools-queue-chip-value">{summary.failed ?? 0}</span>
                                </span>
                            </div>
                        </div>
                    );
                })}
            </div>
        </div>
    );

    const renderBrowserWorkbenchPage = () => {
        const selectedScopeLabel = runAll ? `all filtered photos` : 'selected photos';
        const selectedCountLabel = runAll ? workbenchPhotos.length : selectedVisibleCount;
        return (
            <>
                <div className="tools-panel tools-workbench-panel">
                    <div className="tools-panel-header">
                        <div>
                            <h2 className="tools-panel-title">Browser workbench</h2>
                        </div>
                        <div className="tools-selection-actions">
                            <button type="button" className="btn btn-soft" onClick={() => setRunAll((value) => !value)} aria-pressed={runAll}>
                                {runAll ? `Selected photos (${selectedVisibleCount})` : `All filtered photos (${workbenchPhotos.length})`}
                            </button>
                            <button type="button" className="btn btn-soft" onClick={() => setForceRun((value) => !value)} aria-pressed={forceRun}>
                                {forceRun ? 'Force rerun enabled' : 'Force rerun disabled'}
                            </button>
                            <button type="button" className="btn btn-soft" onClick={clearSelection} disabled={selected.size === 0}>
                                Clear selection
                            </button>
                        </div>
                    </div>

                    <div className="tools-selection-actions" aria-label="Browser processing actions">
                        <button type="button" className="btn btn-soft" onClick={() => void runBrowserAction('thumbnails')} disabled={!!running}>
                            Run thumbnails on {selectedScopeLabel}
                        </button>
                        <button type="button" className="btn btn-soft" onClick={() => void runBrowserAction('exif')} disabled={!!running}>
                            Run EXIF on {selectedScopeLabel}
                        </button>
                        <button type="button" className="btn btn-soft" onClick={() => void runBrowserAction('ocr')} disabled={!!running}>
                            Run OCR on {selectedScopeLabel}
                        </button>
                        <button type="button" className="btn btn-soft" onClick={() => void runBrowserAction('vision')} disabled={!!running}>
                            Run AI vision on {selectedScopeLabel}
                        </button>
                        <button type="button" className="btn btn-soft" onClick={() => void runBrowserAction('map')} disabled={!!running}>
                            Run map tagging on {selectedScopeLabel}
                        </button>
                        <button type="button" className="btn btn-soft" onClick={() => void runBrowserAction('faces')} disabled={!!running}>
                            Run face detection on {selectedScopeLabel}
                        </button>
                    </div>

                    <div className="tools-panel-meta">
                        Scope count: {selectedCountLabel}
                        {selectedOutsideViewCount > 0 ? ` · ${selectedOutsideViewCount} selected outside this view` : ''}
                    </div>

                    <div className="tools-filter-panel">
                        {renderWorkbenchViewToggle()}
                        {renderProcessingFilters()}
                    </div>
                </div>

                <div className="tools-panel tools-gallery-panel">
                    <div className="tools-panel-header">
                        <div>
                            <h2 className="tools-panel-title">Gallery</h2>
                        </div>
                    </div>

                    {loading && <p className="status">Loading photos...</p>}
                    {!loading && workbenchPhotos.length === 0 && <p className="empty">No photos found.</p>}

                    <div className="gallery-grid">
                        {workbenchPhotos.map((photo, index) => {
                            const selectedFlag = selected.has(photo.filename);
                            return (
                                <PhotoTile
                                    key={photo.filename}
                                    photo={photo}
                                    title={photo.filename}
                                    selected={selectedFlag}
                                    onCardClick={() => toggleOne(photo.filename)}
                                    selectableOverlay={(
                                        <input
                                            type="checkbox"
                                            aria-label={`Toggle ${photo.filename}`}
                                            checked={selectedFlag}
                                            onChange={() => toggleOne(photo.filename)}
                                        />
                                    )}
                            bodyContent={(
                                        <>
                                            <div className="tools-photo-status-row" aria-label={`Processing status for ${photo.filename}`}>
                                                <PhotoProcessingChip label="Thumbnail" status={getProcessingStatus(photo, 'thumbnail')} step="thumbnail" />
                                                <PhotoProcessingChip label="EXIF" status={getProcessingStatus(photo, 'exif')} step="exif" />
                                                <PhotoProcessingChip label="OCR" status={getProcessingStatus(photo, 'ocr')} step="ocr" />
                                                <PhotoProcessingChip label="VISION" status={getProcessingStatus(photo, 'aiVision')} step="aiVision" />
                                                <PhotoProcessingChip label="MAP" status={getProcessingStatus(photo, 'mapDetection')} step="mapDetection" />
                                                <PhotoProcessingChip label="FACE" status={getProcessingStatus(photo, 'face')} step="face" />
                                                <button type="button" className="btn btn-soft icon-btn" onClick={(event) => { event.stopPropagation(); void toggleInfo(photo.filename); }} aria-label={`Toggle info for ${photo.filename}`} title={`Toggle info for ${photo.filename}`}>
                                                    <InformationCircleIcon className="toolbar-icon" />
                                                    <span className="sr-only">Toggle info for {photo.filename}</span>
                                                </button>
                                                <button type="button" className="btn btn-soft icon-btn" onClick={(event) => { event.stopPropagation(); setViewerIndex(index); }} aria-label={`Open ${photo.filename}`} title={`Open ${photo.filename}`}>
                                                    <MagnifyingGlassIcon className="toolbar-icon" />
                                                    <span className="sr-only">Open {photo.filename}</span>
                                                </button>
                                            </div>
                                            {renderPhotoInfo(photo)}
                                        </>
                                    )}
                                />
                            );
                        })}
                    </div>
                    {hasMorePhotos && viewMode !== 'recent' && (
                        <div className="tools-load-more">
                            <button
                                type="button"
                                className="btn btn-soft"
                                onClick={() => void loadMorePhotos()}
                                disabled={loadingMore}
                            >
                                {loadingMore ? 'Loading…' : `Load more (${photos.length} of ${photosTotal})`}
                            </button>
                        </div>
                    )}
                </div>

                {viewerIndex !== null && workbenchPhotos[viewerIndex] && (
                    <PhotoViewer
                        photos={workbenchPhotos}
                        index={viewerIndex}
                        onClose={() => setViewerIndex(null)}
                        onIndexChange={(index: number) => setViewerIndex(index)}
                    />
                )}
            </>
        );
    };

    const renderRecoveryPage = () => (
        <>
            <div className="tools-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Recovery checklist</h2>
                    </div>
                    <div className="tools-hero-actions">
                        <Link to="/tools/queue-status" className="btn btn-soft">
                            Queue status
                        </Link>
                    </div>
                </div>
            </div>

            <details open className="tools-panel tools-admin">
                <summary className="tools-admin-summary">
                    <div>
                        <div className="tools-panel-title">Recovery</div>
                    </div>
                    <span className="tools-admin-summary-badge">Use sparingly</span>
                </summary>
                <div className="tools-admin-body">
                    <div className="tools-admin-callout">
                        <ExclamationTriangleIcon className="tools-admin-callout-icon" />
                        <p>
                            Only use repair actions when clustering or the vector index needs a snapshot-backed reset.
                        </p>
                    </div>
                    <div className="tools-admin-grid">
                        <button type="button" className="btn btn-soft icon-btn tools-admin-button" onClick={() => void runReclusterPeople()} disabled={!!running} aria-label="Protected people recluster repair" title="Protected people recluster repair">
                            <UsersIcon className="toolbar-icon" />
                            <span className="sr-only">Protected people recluster repair</span>
                        </button>
                        <button type="button" className="btn btn-soft icon-btn tools-admin-button" onClick={() => void runRebuildVectorIndex()} disabled={!!running} aria-label="Rebuild vector index" title="Rebuild vector index">
                            <ArrowPathIcon className="toolbar-icon" />
                            <span className="sr-only">Rebuild vector index</span>
                        </button>
                        <Link to="/people" className="btn btn-soft icon-btn tools-admin-link" aria-label="Open People page" title="Open People page">
                            <UserCircleIcon className="toolbar-icon" />
                            <span className="sr-only">Open People page</span>
                        </Link>
                    </div>
                </div>
            </details>

            <div className="tools-workbench-footnote">
                <div className="tools-selection-actions">
                    <Link to="/tools/queue-status" className="tools-inline-link">Open queue status</Link>
                </div>
            </div>
        </>
    );

    return (
        <section className="gallery-wrap card-glass tools-wrap">
            {isOverviewPage && renderOverviewPage()}
            {isQueueStatusPage && renderQueueStatusPage()}
            {isBrowserWorkbenchPage && renderBrowserWorkbenchPage()}
            {isRecoveryPage && renderRecoveryPage()}
            {running && <p className="status">Queueing '{running}'...</p>}
            {message && <p className={`status ${message.includes('failed') ? 'error' : 'success'}`}>{message}</p>}
        </section>
    );
};

export default ToolsPage;
