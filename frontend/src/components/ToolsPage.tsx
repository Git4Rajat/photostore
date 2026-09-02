import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
    ArrowPathIcon,
    ArrowUturnLeftIcon,
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
    TrashIcon,
    UserCircleIcon,
    UsersIcon,
} from '@heroicons/react/24/outline';
import { get, post } from '../services/apiClient';
import { getRuntimeConfig } from '../config/appConfig';
import { requestJobPoll } from '../services/jobNotifications';
import { plural } from '../utils/format';
import { confirmDialog } from './shared/dialogs';
import { useAppServices, browserProcessingActionSteps } from './AppServicesProvider';
import type { BrowserProcessingAction } from './AppServicesProvider';
import PhotoTile from './shared/PhotoTile';
import { useDragSelect } from '../services/useDragSelect';
import PhotoQuickActions, { libraryFocusHref } from './shared/PhotoQuickActions';
import PhotoActionSheet from './shared/PhotoActionSheet';
import PhotoViewer from './shared/PhotoViewer';
import type { PhotoPersonLink } from '../types/uiTypes';
import { EmptyState } from './shared/EmptyState';
import { Loading } from './shared/Loading';
import { classifyApiError, type ApiError } from '../services/apiError';
import { useBackendRecoveryRetry } from '../services/useBackendRecoveryRetry';

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
    people?: PhotoPersonLink[];
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

type ToolAction = BrowserProcessingAction | 'peopleIndex' | 'vectorIndex' | 'suppressSuspicious' | 'dedupeFaces' | 'rebuildPeopleIndex' | 'repairMemberships' | 'unblockFaces' | 'backfillPhotos' | 'purgeOrphaned' | 'restoreSnapshot';
type ProcessingFilterState = 'all' | 'failed' | 'no_data';
type ProcessingFilterProcess = 'all' | 'thumbnail' | 'exif' | 'ocr' | 'aiVision' | 'mapDetection' | 'face';
type ChipStepKey = 'thumbnail' | 'exif' | 'ocr' | 'aiVision' | 'mapDetection' | 'face';
type QueueStageKey = keyof QueueStatus;
type WorkbenchViewKey = 'recent' | 'all' | 'attention';
type WorkbenchScope = 'selected' | 'view' | 'library';

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

const browserActionButtons: Array<{ action: BrowserProcessingAction; label: string; icon: React.ComponentType<React.SVGProps<SVGSVGElement>> }> = [
    { action: 'thumbnails', label: 'Thumbnails', icon: processingStageIcons.thumbnail },
    { action: 'exif', label: 'EXIF', icon: processingStageIcons.exif },
    { action: 'ocr', label: 'OCR', icon: processingStageIcons.ocr },
    { action: 'vision', label: 'AI vision', icon: processingStageIcons.aiVision },
    { action: 'map', label: 'Map tagging', icon: processingStageIcons.mapDetection },
    { action: 'faces', label: 'Faces', icon: processingStageIcons.face },
];

const runningActionLabels: Record<ToolAction, string> = {
    thumbnails: 'thumbnail generation',
    exif: 'EXIF extraction',
    ocr: 'OCR',
    vision: 'AI vision',
    map: 'map tagging',
    faces: 'face detection',
    peopleIndex: 'people recluster',
    vectorIndex: 'vector index rebuild',
    suppressSuspicious: 'suppress suspicious faces',
    dedupeFaces: 'deduplicate faces',
    rebuildPeopleIndex: 'rebuild people index',
    repairMemberships: 'repair stale memberships',
    unblockFaces: 'unblock low-confidence faces',
    backfillPhotos: 'full library backfill',
    purgeOrphaned: 'purge orphaned photo data',
    restoreSnapshot: 'snapshot restore',
};

interface AdminDryRunPreview {
    action: 'suppressSuspicious' | 'dedupeFaces' | 'rebuildPeopleIndex' | 'repairMemberships' | 'unblockFaces' | 'purgeOrphaned';
    result: Record<string, unknown>;
}

interface LastSnapshot {
    label: string;
    snapshotId: string;
    createdAt: number;
}

const toolsSubnavItems: Array<{ key: ToolsPageKey; to: string; label: string; note: string }> = [
    { key: 'overview', to: '/tools', label: 'Overview', note: 'Queues, quick actions, photos' },
    { key: 'browser-workbench', to: '/tools/browser-workbench', label: 'Workbench', note: 'Bulk re-run processing steps' },
    { key: 'queue-status', to: '/tools/queue-status', label: 'Queue status', note: 'Live pipeline counters' },
    { key: 'recovery', to: '/tools/recovery', label: 'Recovery', note: 'Snapshot-backed repairs' },
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
    const [dryRunPreview, setDryRunPreview] = useState<AdminDryRunPreview | null>(null);
    const [lastSnapshot, setLastSnapshot] = useState<LastSnapshot | null>(null);
    const [queueLoadWarning, setQueueLoadWarning] = useState<string>('');
    const [expandedInfo, setExpandedInfo] = useState<Set<string>>(new Set());
    const [infoByFile, setInfoByFile] = useState<Record<string, ToolPhotoMetadata>>({});
    const [loadingInfo, setLoadingInfo] = useState<Set<string>>(new Set());
    const [queueStatus, setQueueStatus] = useState<QueueStatus>({});
    const [queueUpdatedAt, setQueueUpdatedAt] = useState<Date | null>(null);
    const [isQueueStatusExpanded, setIsQueueStatusExpanded] = useState<boolean>(true);
    const [forceRun, setForceRun] = useState<boolean>(false);
    const [scope, setScope] = useState<WorkbenchScope>('selected');
    const [processingFilterState, setProcessingFilterState] = useState<ProcessingFilterState>('all');
    const [processingFilterProcess, setProcessingFilterProcess] = useState<ProcessingFilterProcess>('all');
    const [viewMode, setViewMode] = useState<WorkbenchViewKey>('recent');
    const [viewerIndex, setViewerIndex] = useState<number | null>(null);
    const [photosLoadError, setPhotosLoadError] = useState<ApiError | null>(null);
    const [pendingFocusFilename, setPendingFocusFilename] = useState<string | null>(null);
    const focusTargetRef = useRef<HTMLDivElement | null>(null);
    const [actionSheetTarget, setActionSheetTarget] = useState<{ filenames: string[]; people?: Photo['people'] } | null>(null);
    const activeToolsPage = getToolsPageKey(location.pathname);
    const isOverviewPage = activeToolsPage === 'overview';
    const isQueueStatusPage = activeToolsPage === 'queue-status';
    const isBrowserWorkbenchPage = activeToolsPage === 'browser-workbench';
    const isRecoveryPage = activeToolsPage === 'recovery';

    const loadPhotos = async (queryText: string = '') => {
        setLoading(true);
        setMessage('');
        setPhotosLoadError(null);
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
            setPhotosLoadError(classifyApiError(err));
        } finally {
            setLoading(false);
        }
    };

    useBackendRecoveryRetry(photosLoadError, () => { void loadPhotos(); });

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

    // Re-fetches just the acted-on photos and patches them into the existing
    // list in place, instead of loadPhotos()'s full page-0 reset -- that reset
    // used to throw away pagination/view state and could drop the very photo
    // the user just ran an action on (e.g. an older photo outside the first
    // page, or filtered out of 'recent'/'attention' once its status changed),
    // making it look like the run did nothing.
    const refreshPhotosByFilename = async (filenames: string[]) => {
        const updates = await Promise.all(filenames.map(async (filename) => {
            try {
                const response = await get(`/photos/lookup/${encodeURIComponent(filename)}`);
                return response?.photo as Photo | undefined;
            } catch {
                return undefined;
            }
        }));
        setPhotos((prev) => prev.map((photo) => {
            const updated = updates.find((candidate) => candidate?.filename === photo.filename);
            return updated || photo;
        }));
    };

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
            setQueueUpdatedAt(new Date());
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

    // "Open in Workbench" deep link from another page: pull that one photo in
    // (it may not be on the first loaded page), select it, and switch to the
    // 'all' view so it isn't hidden by the 'recent'/'attention' filters.
    useEffect(() => {
        if (!isBrowserWorkbenchPage) {
            return;
        }
        const params = new URLSearchParams(location.search);
        const target = params.get('filename');
        if (!target) {
            return;
        }
        setPendingFocusFilename(target);
        setViewMode('all');
        setSelected((prev) => (prev.has(target) ? prev : new Set(prev).add(target)));
        void (async () => {
            try {
                const response = await get(`/photos/lookup/${encodeURIComponent(target)}`);
                if (response?.photo) {
                    setPhotos((prev) => (prev.some((p) => p.filename === target) ? prev : [response.photo as Photo, ...prev]));
                }
            } catch {
                // Best effort — if the lookup fails the photo simply won't be pre-loaded.
            }
        })();
    }, [isBrowserWorkbenchPage, location.search]);

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

    useEffect(() => {
        if (!pendingFocusFilename || !workbenchPhotos.some((photo) => photo.filename === pendingFocusFilename)) {
            return undefined;
        }
        const timer = window.setTimeout(() => {
            focusTargetRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 50);
        return () => window.clearTimeout(timer);
    }, [pendingFocusFilename, workbenchPhotos]);

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
    const dragSelectHandlers = useDragSelect({
        isSelected: (filename) => selected.has(filename),
        setSelected: (filename, isOn) => {
            setSelected(prev => {
                if (isOn === prev.has(filename)) {
                    return prev;
                }
                const next = new Set(prev);
                if (isOn) {
                    next.add(filename);
                } else {
                    next.delete(filename);
                }
                return next;
            });
        },
    });
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
                {loadingInfo.has(photo.filename) && <span className="tools-info-muted">Loading…</span>}
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

    // Queues a single step for every non-deleted, non-video photo in the account
    // (server-side, not limited to whatever page of `photos` happens to be loaded
    // in this tab) and then kicks off the browser's background processing pull.
    // Mirrors runBackfillPhotos() but scoped to one step via the 'steps' param.
    const runBrowserActionOnLibrary = async (action: BrowserProcessingAction) => {
        if (!(await confirmDialog({
            title: 'Run on entire library',
            message: `Re-run ${runningActionLabels[action]} on every photo in your library, including ones already processed? The browser will work through them in the background. This can take a while for large libraries.`,
            confirmLabel: 'Run on all photos',
            danger: false,
        }))) {
            return;
        }
        setRunning(action);
        setMessage(`Queueing ${runningActionLabels[action]} for the entire library…`);
        try {
            const response = await post('/api/admin/backfill/photos', {
                repair: true,
                confirm: 'BACKFILL_ALL_PHOTOS',
                steps: browserProcessingActionSteps[action],
            });
            const queued = Number(response?.queued ?? 0);
            const skipped = Number(response?.skipped ?? 0);
            setMessage(`${runningActionLabels[action]} queued for ${plural(queued, 'photo')}${skipped > 0 ? `, ${skipped} skipped (videos / deleted)` : ''}. Processing will start in the background.`);
            await loadQueueStatus();
            // Same reasoning as runBackfillPhotos(): without this, a freshly opened
            // Tools tab would queue the work server-side but never actually start
            // processing it.
            if (browserAiModelState.status !== 'available') {
                await loadBrowserAiModel();
            }
            void startBrowserProcessing();
        } catch (err) {
            setMessage(`Library-wide '${action}' failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const runBrowserAction = async (action: BrowserProcessingAction) => {
        if (isBrowserWorkbenchPage && scope === 'library') {
            await runBrowserActionOnLibrary(action);
            return;
        }
        const actionPhotos = isBrowserWorkbenchPage ? workbenchPhotos : photos;
        const useAllMatching = isBrowserWorkbenchPage && scope === 'view';
        const selectedPhotos = useAllMatching
            ? actionPhotos
            : actionPhotos.filter((photo) => selected.has(photo.filename));
        if (selectedPhotos.length === 0) {
            setMessage(useAllMatching
                ? 'No photos match the current filters.'
                : (isBrowserWorkbenchPage
                    ? 'Select photos below, or switch the scope to all filtered photos.'
                    : 'Select photos below first, then run a step.'));
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
            const processingMode = getRuntimeConfig().processingMode || 'browser';
            if (action === 'vision' && processingMode !== 'backend' && browserAiModelState.status !== 'available') {
                const modelState = await loadBrowserAiModel();
                if (modelState.status !== 'available') {
                    setMessage(`Browser AI is not available: ${modelState.detail || modelState.reason || modelState.status}.`);
                    return;
                }
            }
            setMessage(`Running ${runningActionLabels[action]} on ${plural(filenames.length, 'photo')}…`);
            // PROCESSING_MODE 'backend'/'both': startBrowserProcessing alone can no
            // longer (fully) do this step -- it self-gates every step to a no-op
            // once processingMode !== 'browser' (see runBrowserProcessing in
            // PhotoGallery.tsx). Also enqueue real ipworker jobs for exactly this
            // selection, mirroring what runBrowserActionOnLibrary already does for
            // the "entire library" scope. Best-effort: a network failure here
            // shouldn't block the in-browser run below, which still works
            // standalone in 'both' mode.
            let ipworkQueued = 0;
            if (processingMode !== 'browser') {
                try {
                    const ipworkResponse = await post('/api/admin/ipwork/enqueue', {
                        filenames,
                        steps: browserProcessingActionSteps[action],
                        force: forceRun,
                    });
                    ipworkQueued = Number(ipworkResponse?.queued ?? 0);
                } catch (err) {
                    setMessage(`Backend enqueue for '${action}' failed: ${String(err)}`);
                }
            }
            const processed = await startBrowserProcessing({
                actions: [action],
                filenames,
                items,
                force: forceRun,
            });
            const backendNote = ipworkQueued > 0 ? ` ${plural(ipworkQueued, 'photo')} queued to the backend.` : '';
            setMessage(`Finished ${runningActionLabels[action]}: ${processed} of ${plural(filenames.length, 'photo')} processed in-browser.${backendNote}`);
            await refreshPhotosByFilename(filenames);
            await loadQueueStatus();
        } catch (err) {
            setMessage(`Browser '${action}' failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    // The recluster repair snapshots before it runs, but it runs as a queued
    // background job — the snapshot id only appears once the job finishes. Poll
    // the existing job-status endpoint briefly to pick it up so the Restore
    // affordance below can offer it, same as the synchronous repair actions do.
    const pollJobForSnapshot = (jobId: string, label: string) => {
        let attempts = 0;
        const maxAttempts = 12;
        const tick = async () => {
            attempts += 1;
            try {
                const response = await get('/api/jobs/status') as { jobs?: Array<Record<string, unknown>> };
                const job = (response?.jobs || []).find((j) => j.jobId === jobId);
                if (job && (job.status === 'done' || job.status === 'failed')) {
                    const snapshotId = String(job.snapshotId || '');
                    if (snapshotId) {
                        setLastSnapshot({ label, snapshotId, createdAt: Date.now() });
                    }
                    return;
                }
            } catch {
                return;
            }
            if (attempts < maxAttempts) {
                window.setTimeout(() => void tick(), 3000);
            }
        };
        void tick();
    };

    const runReclusterPeople = async () => {
        if (!(await confirmDialog({
            title: 'Recovery action',
            message: 'Run protected people recluster repair? Current assignments will be snapshotted first and can be restored afterward, but this should only be used for admin recovery.',
            confirmLabel: 'Run repair',
            danger: true,
        }))) {
            return;
        }
        setRunning('peopleIndex');
        setMessage('Preparing protected people repair…');
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
                requestJobPoll();
                if (response?.jobId) {
                    pollJobForSnapshot(String(response.jobId), 'Repair people clusters');
                }
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

    const runAdminDryRun = async (action: AdminDryRunPreview['action']) => {
        const configs: Record<AdminDryRunPreview['action'], { endpoint: string; confirm: string; label: string }> = {
            suppressSuspicious: { endpoint: '/api/admin/people/suppress-suspicious-faces', confirm: 'SUPPRESS_SUSPICIOUS_FACES', label: 'Suppress suspicious faces' },
            dedupeFaces: { endpoint: '/api/admin/people/dedupe-faces', confirm: 'DEDUPE_FACES', label: 'Deduplicate faces' },
            rebuildPeopleIndex: { endpoint: '/api/admin/people/rebuild-photo-people-index', confirm: 'REBUILD_PEOPLE_INDEX', label: 'Rebuild people index' },
            repairMemberships: { endpoint: '/api/admin/people/repair-stale-memberships', confirm: 'REPAIR_STALE_MEMBERSHIPS', label: 'Repair stale memberships' },
            unblockFaces: { endpoint: '/api/admin/people/unblock-low-confidence-faces', confirm: 'UNBLOCK_LOW_CONFIDENCE_FACES', label: 'Unblock low-confidence faces' },
            purgeOrphaned: { endpoint: '/api/admin/photos/purge-orphaned-data', confirm: 'PURGE_ORPHANED_PHOTO_DATA', label: 'Purge orphaned photo data' },
        };
        const cfg = configs[action];
        setRunning(action);
        setDryRunPreview(null);
        setMessage(`Previewing ${cfg.label}…`);
        try {
            const result = await post(cfg.endpoint, { repair: true, confirm: cfg.confirm, dryRun: true }) as Record<string, unknown>;
            setDryRunPreview({ action, result });
            setMessage('');
        } catch (err) {
            setMessage(`Preview failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const runAdminApply = async (action: AdminDryRunPreview['action']) => {
        const configs: Record<AdminDryRunPreview['action'], { endpoint: string; confirm: string; label: string; warning: string }> = {
            suppressSuspicious: {
                endpoint: '/api/admin/people/suppress-suspicious-faces',
                confirm: 'SUPPRESS_SUSPICIOUS_FACES',
                label: 'Suppress suspicious faces',
                warning: 'This will mark low-confidence faces as rejected/suspicious based on the current threshold settings. Rejected faces are excluded from clustering. A snapshot is saved first — use Restore below to undo.',
            },
            dedupeFaces: {
                endpoint: '/api/admin/people/dedupe-faces',
                confirm: 'DEDUPE_FACES',
                label: 'Deduplicate faces',
                warning: 'This will remove duplicate face rows for the same detected face. A snapshot is saved first — use Restore below to undo.',
            },
            rebuildPeopleIndex: {
                endpoint: '/api/admin/people/rebuild-photo-people-index',
                confirm: 'REBUILD_PEOPLE_INDEX',
                label: 'Rebuild people index',
                warning: 'This rebuilds the photo→person mapping index. Use this if people are not appearing on their photos.',
            },
            repairMemberships: {
                endpoint: '/api/admin/people/repair-stale-memberships',
                confirm: 'REPAIR_STALE_MEMBERSHIPS',
                label: 'Repair stale memberships',
                warning: 'This fixes faces that are listed in a person cluster they no longer belong to. A snapshot is saved first — use Restore below to undo.',
            },
            unblockFaces: {
                endpoint: '/api/admin/people/unblock-low-confidence-faces',
                confirm: 'UNBLOCK_LOW_CONFIDENCE_FACES',
                label: 'Unblock low-confidence faces',
                warning: 'This un-rejects faces that were auto-suppressed as low-confidence but now meet the current (lowered) threshold. Run this after lowering FACE_LOW_CONFIDENCE_REJECT_BELOW to bring previously-rejected faces back into clustering. A snapshot is saved first — use Restore below to undo.',
            },
            purgeOrphaned: {
                endpoint: '/api/admin/photos/purge-orphaned-data',
                confirm: 'PURGE_ORPHANED_PHOTO_DATA',
                label: 'Purge orphaned photo data',
                warning: 'This deletes metadata rows, face rows, and person records for photos whose image blob no longer exists in storage. Uses blob storage as the source of truth. No snapshot is saved for this action — it cannot be undone.',
            },
        };
        const cfg = configs[action];
        if (!(await confirmDialog({
            title: `Apply: ${cfg.label}`,
            message: cfg.warning,
            confirmLabel: 'Apply now',
            danger: true,
        }))) return;
        setRunning(action);
        setDryRunPreview(null);
        setMessage(`Applying ${cfg.label}…`);
        try {
            const result = await post(cfg.endpoint, { repair: true, confirm: cfg.confirm, dryRun: false }) as Record<string, unknown>;
            const affectedCountFields: Record<AdminDryRunPreview['action'], string> = {
                suppressSuspicious: 'markedSuspicious',
                dedupeFaces: 'deletedFaces',
                rebuildPeopleIndex: 'updatedFiles',
                repairMemberships: 'updatedPeople',
                unblockFaces: 'unblockedFaces',
                purgeOrphaned: 'orphanedMetadataDeleted',
            };
            const affected = Number(result?.[affectedCountFields[action]] ?? 0);
            const snapshotId = String(result?.snapshotId || '');
            if (snapshotId) {
                setLastSnapshot({ label: cfg.label, snapshotId, createdAt: Date.now() });
            }
            setMessage(`${cfg.label} complete. ${affected} affected.${snapshotId ? ' A snapshot was saved — see Restore below to undo.' : ''}`);
            await loadQueueStatus();
        } catch (err) {
            setMessage(`${cfg.label} failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const runRestoreSnapshot = async () => {
        if (!lastSnapshot) return;
        if (!(await confirmDialog({
            title: 'Restore snapshot',
            message: `Restore people and face assignments to how they were before "${lastSnapshot.label}"? This overwrites current person/face state with the saved snapshot.`,
            confirmLabel: 'Restore',
            danger: true,
        }))) {
            return;
        }
        setRunning('restoreSnapshot');
        setMessage('Restoring snapshot…');
        try {
            const result = await post('/api/admin/people/recluster/restore', { snapshotId: lastSnapshot.snapshotId }) as Record<string, unknown>;
            if (result?.success) {
                setMessage(`Snapshot restored: ${Number(result?.restoredPeople || 0)} people, ${Number(result?.restoredFaces || 0)} faces, ${Number(result?.restoredMetadata || 0)} metadata rows.`);
                setLastSnapshot(null);
                await loadQueueStatus();
            } else {
                setMessage(`Restore failed: ${String(result?.error || 'unknown error')}`);
            }
        } catch (err) {
            setMessage(`Restore failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const runBackfillPhotos = async () => {
        if (!(await confirmDialog({
            title: 'Backfill all photos',
            message: 'Re-process every photo in your library from scratch? This resets thumbnails, EXIF, OCR, AI vision, map tagging, and face detection for all photos and re-runs them through the full pipeline — the same as a fresh upload. The browser will work through them in the background. This can take a while for large libraries.',
            confirmLabel: 'Start backfill',
            danger: false,
        }))) {
            return;
        }
        setRunning('backfillPhotos');
        setMessage('Queueing all photos for backfill…');
        try {
            const response = await post('/api/admin/backfill/photos', {
                repair: true,
                confirm: 'BACKFILL_ALL_PHOTOS',
            });
            const queued = Number(response?.queued ?? 0);
            const skipped = Number(response?.skipped ?? 0);
            setMessage(`Backfill queued: ${plural(queued, 'photo')} queued${skipped > 0 ? `, ${skipped} skipped (videos / deleted)` : ''}. Processing will start in the background.`);
            await loadQueueStatus();
            // startBrowserProcessing()'s automatic-pull path no-ops until browser AI is
            // loaded (see AppServicesProvider) -- without this, a freshly opened Tools
            // tab would queue the backfill server-side but never actually start
            // processing (not even AI-independent steps like thumbnails/EXIF).
            if (browserAiModelState.status !== 'available') {
                await loadBrowserAiModel();
            }
            void startBrowserProcessing();
        } catch (err) {
            setMessage(`Backfill failed: ${String(err)}`);
        } finally {
            setRunning(null);
        }
    };

    const runRebuildVectorIndex = async () => {
        if (!(await confirmDialog({
            title: 'Recovery action',
            message: 'Rebuild the cached vector index for this account? This refreshes the on-disk index from the latest face embeddings.',
            confirmLabel: 'Rebuild index',
        }))) {
            return;
        }
        setRunning('vectorIndex');
        setMessage('Rebuilding vector index…');
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

    const queueUpdatedLabel = queueUpdatedAt
        ? queueUpdatedAt.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' })
        : '';

    const renderToolsHeader = () => (
        <>
            <header className="tools-header">
                <div>
                    <h1 className="tools-title">Tools</h1>
                    <p className="tools-subtitle">
                        Keepsake processes photos on your device — Thumbnails, EXIF, OCR, AI vision, Map tagging, and Faces.
                        Watch the queues and re-run steps here.
                    </p>
                </div>
            </header>
            <nav className="tools-subnav" aria-label="Tools sections">
                {toolsSubnavItems.map((item) => (
                    <Link
                        key={item.key}
                        to={item.to}
                        className={`tools-subnav-link${activeToolsPage === item.key ? ' active' : ''}`}
                        aria-current={activeToolsPage === item.key ? 'page' : undefined}
                    >
                        <span className="tools-subnav-label">{item.label}</span>
                        <span className="tools-subnav-note">{item.note}</span>
                    </Link>
                ))}
            </nav>
        </>
    );

    const statusText = message || (running ? `Running ${runningActionLabels[running]}…` : '');
    const isErrorStatus = /fail|unavailable|not available/i.test(statusText);

    const renderStatusArea = () => (
        <div className="tools-status-area" role="status" aria-live="polite">
            {statusText && (
                <p className={`status ${isErrorStatus ? 'error' : 'success'}`}>{statusText}</p>
            )}
        </div>
    );

    const renderQueueGrid = () => (
        <div className="tools-queue-grid">
            {queueStageCards.map((item) => {
                const summary = queueStatus[item.key] || {};
                const counts = [
                    { label: 'waiting', value: Number(summary.pendingTotal ?? 0), icon: ClockIcon, tone: '' },
                    { label: 'running', value: Number(summary.running ?? 0), icon: ArrowPathIcon, tone: '' },
                    { label: 'no data', value: Number(summary.noData ?? 0), icon: InformationCircleIcon, tone: 'warning' },
                    { label: 'failed', value: Number(summary.failed ?? 0), icon: ExclamationTriangleIcon, tone: 'bad' },
                ];
                return (
                    <div key={item.key} className="tools-queue-card">
                        <div className="tools-queue-card-top">
                            <span className="tools-queue-icon" aria-hidden="true">
                                <item.icon className="tools-queue-icon-svg" />
                            </span>
                            <div className="tools-queue-card-copy">
                                <span className="tools-queue-card-title">{item.label}</span>
                                <span className="tools-queue-card-desc">{item.description}</span>
                            </div>
                        </div>
                        <div className="tools-queue-counts">
                            {counts.map((count) => (
                                <span
                                    key={count.label}
                                    className={`tools-queue-chip${count.value === 0 ? ' is-zero' : count.tone ? ` tone-${count.tone}` : ''}`}
                                    title={`${item.label}: ${count.value} ${count.label}`}
                                >
                                    <count.icon className="tools-queue-chip-icon" aria-hidden="true" />
                                    <span className="tools-queue-chip-value">{count.value}</span>
                                    <span className="tools-queue-chip-label">{count.label}</span>
                                </span>
                            ))}
                        </div>
                    </div>
                );
            })}
        </div>
    );

    const renderBrowserActionButtons = () => (
        <div className="tools-action-grid" aria-label="Processing steps">
            {browserActionButtons.map((item) => (
                <button
                    key={item.action}
                    type="button"
                    className="btn btn-soft tools-action-button"
                    onClick={() => void runBrowserAction(item.action)}
                    disabled={!!running}
                    aria-busy={running === item.action}
                >
                    <item.icon className="toolbar-icon" aria-hidden="true" />
                    {running === item.action ? 'Running…' : item.label}
                </button>
            ))}
        </div>
    );

    const renderForceToggle = () => (
        <button
            type="button"
            className={`btn btn-soft tools-force-button${forceRun ? ' active' : ''}`}
            onClick={() => setForceRun((value) => !value)}
            aria-pressed={forceRun}
            title="Re-run steps even on photos that already have results"
        >
            <BoltIcon className="toolbar-icon" aria-hidden="true" />
            {forceRun ? 'Force re-run: on' : 'Force re-run: off'}
        </button>
    );

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

    const renderPhotoStatusRow = (photo: Photo, index: number, withOpenButton: boolean) => (
        <div className="tools-photo-status-row" aria-label={`Processing status for ${photo.filename}`}>
            <PhotoProcessingChip label="Thumbnail" status={getProcessingStatus(photo, 'thumbnail')} step="thumbnail" />
            <PhotoProcessingChip label="EXIF" status={getProcessingStatus(photo, 'exif')} step="exif" />
            <PhotoProcessingChip label="OCR" status={getProcessingStatus(photo, 'ocr')} step="ocr" />
            <PhotoProcessingChip label="Vision" status={getProcessingStatus(photo, 'aiVision')} step="aiVision" />
            <PhotoProcessingChip label="Map" status={getProcessingStatus(photo, 'mapDetection')} step="mapDetection" />
            <PhotoProcessingChip label="Face" status={getProcessingStatus(photo, 'face')} step="face" />
            <button type="button" className="btn btn-soft icon-btn" onClick={(event) => { event.stopPropagation(); void toggleInfo(photo.filename); }} aria-label={`Toggle info for ${photo.filename}`} title={`Toggle info for ${photo.filename}`}>
                <InformationCircleIcon className="toolbar-icon" />
                <span className="sr-only">Toggle info for {photo.filename}</span>
            </button>
            {withOpenButton && (
                <button type="button" className="btn btn-soft icon-btn" onClick={(event) => { event.stopPropagation(); setViewerIndex(index); }} aria-label={`Open ${photo.filename}`} title={`Open ${photo.filename}`}>
                    <MagnifyingGlassIcon className="toolbar-icon" />
                    <span className="sr-only">Open {photo.filename}</span>
                </button>
            )}
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
                        <p className="tools-panel-meta">
                            {queueUpdatedLabel ? `Updated ${queueUpdatedLabel} · ` : ''}refreshes every 30 seconds
                        </p>
                    </div>
                    <button type="button" className="btn btn-soft icon-btn" onClick={(event) => { event.preventDefault(); void loadQueueStatus(); }} aria-label="Refresh queue status" title="Refresh queue status">
                        <ArrowPathIcon className="toolbar-icon" />
                        <span className="sr-only">Refresh queue status</span>
                    </button>
                </summary>
                {queueLoadWarning && <p className="status error tools-queue-warning">{queueLoadWarning}</p>}
                {renderQueueGrid()}
            </details>

            <div className="tools-panel tools-actions-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Run processing</h2>
                        <p className="tools-panel-meta">
                            {selected.size > 0
                                ? `Runs on the ${plural(selected.size, 'selected photo')} below.`
                                : 'Select photos below, then choose a step to run in this browser.'}
                        </p>
                    </div>
                    {renderForceToggle()}
                </div>
                {renderBrowserActionButtons()}
            </div>

            <div className="tools-panel tools-gallery-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Photos</h2>
                        <p className="tools-panel-meta">{plural(overviewPhotos.length, 'photo')} shown · {selected.size} selected</p>
                    </div>
                    {renderWorkbenchViewToggle()}
                </div>
                <div className="tools-filter-panel">
                    {renderProcessingFilters()}
                    <div className="tools-selection-actions">
                        <button
                            type="button"
                            className="btn btn-soft"
                            onClick={selectAllPreview}
                            disabled={overviewPhotos.length === 0}
                            aria-label={`Select all ${workbenchViewLabels[viewMode].toLowerCase()}`}
                        >
                            Select shown
                        </button>
                        <button type="button" className="btn btn-soft" onClick={clearSelection} disabled={selected.size === 0}>
                            Clear selection
                        </button>
                    </div>
                </div>
                {loading && <Loading label="Loading photos…" fullPage={false} />}
                {!loading && photos.length === 0 && (
                    <EmptyState
                        icon={<PhotoIcon />}
                        title="No photos yet"
                        message="Upload photos from the Gallery and their processing status will appear here."
                    />
                )}
                {!loading && photos.length > 0 && overviewPhotos.length === 0 && <p className="empty">No photos match the current filters.</p>}
                <div className="gallery-grid">
                    {overviewPhotos.map((photo, index) => (
                        <PhotoTile
                            key={photo.filename}
                            photo={photo}
                            title={photo.filename}
                            selected={selected.has(photo.filename)}
                            selectableOverlay={(
                                <input
                                    type="checkbox"
                                    aria-label={`Toggle ${photo.filename}`}
                                    checked={selected.has(photo.filename)}
                                    onClick={(event) => event.stopPropagation()}
                                    onChange={() => toggleOne(photo.filename)}
                                    style={{ touchAction: 'none' }}
                                    {...dragSelectHandlers}
                                />
                            )}
                            onCardClick={() => setViewerIndex(index)}
                            bodyContent={(
                                <>
                                    {renderPhotoStatusRow(photo, index, false)}
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
                    <p className="tools-panel-meta">
                        {queueUpdatedLabel ? `Updated ${queueUpdatedLabel} · ` : ''}refreshes every 30 seconds
                    </p>
                </div>
                <button type="button" className="btn btn-soft icon-btn" onClick={() => void loadQueueStatus()} aria-label="Refresh queue status" title="Refresh queue status">
                    <ArrowPathIcon className="toolbar-icon" />
                    <span className="sr-only">Refresh queue status</span>
                </button>
            </div>
            {queueLoadWarning && <p className="status error">{queueLoadWarning}</p>}
            {renderQueueGrid()}
        </div>
    );

    const renderBrowserWorkbenchPage = () => (
        <>
            <div className="tools-panel tools-workbench-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Browser workbench</h2>
                        <p className="tools-panel-meta">Re-run processing steps on many photos at once.</p>
                    </div>
                </div>

                <div className="tools-controls-grid">
                    <div className="tools-control-group">
                        <span className="tools-control-label">Scope</span>
                        <div className="tools-scope-group" role="group" aria-label="Processing scope">
                            <button
                                type="button"
                                className={`btn btn-soft tools-scope-button${scope === 'selected' ? ' active' : ''}`}
                                onClick={() => setScope('selected')}
                                aria-pressed={scope === 'selected'}
                            >
                                Selected ({selectedVisibleCount})
                            </button>
                            <button
                                type="button"
                                className={`btn btn-soft tools-scope-button${scope === 'view' ? ' active' : ''}`}
                                onClick={() => setScope('view')}
                                aria-pressed={scope === 'view'}
                            >
                                All in view ({workbenchPhotos.length})
                            </button>
                            <button
                                type="button"
                                className={`btn btn-soft tools-scope-button${scope === 'library' ? ' active' : ''}`}
                                onClick={() => setScope('library')}
                                aria-pressed={scope === 'library'}
                                title="Queue a step across every photo in your library, not just what's loaded on this page"
                            >
                                Entire library ({photosTotal})
                            </button>
                        </div>
                        {scope === 'selected' && selectedOutsideViewCount > 0 && (
                            <p className="tools-control-note">
                                {plural(selectedOutsideViewCount, 'selected photo')} hidden by the current view will be skipped.
                            </p>
                        )}
                        {scope === 'library' && (
                            <p className="tools-control-note">
                                Runs the chosen step on every photo in your library (loaded or not), including ones already processed.
                            </p>
                        )}
                    </div>
                    <div className="tools-control-group">
                        <span className="tools-control-label">Options</span>
                        <div className="tools-control-row">
                            {scope === 'library' ? (
                                <span className="tools-control-note">Entire-library runs always re-process, regardless of Force re-run.</span>
                            ) : renderForceToggle()}
                            <button type="button" className="btn btn-soft" onClick={clearSelection} disabled={selected.size === 0}>
                                Clear selection
                            </button>
                        </div>
                    </div>
                    <div className="tools-control-group">
                        <span className="tools-control-label">View</span>
                        {renderWorkbenchViewToggle()}
                    </div>
                    <div className="tools-control-group">
                        <span className="tools-control-label">Filters</span>
                        {renderProcessingFilters()}
                    </div>
                </div>

                <div className="tools-control-group">
                    <span className="tools-control-label">Run a step</span>
                    {renderBrowserActionButtons()}
                </div>
            </div>

            <div className="tools-panel tools-gallery-panel">
                <div className="tools-panel-header">
                    <div>
                        <h2 className="tools-panel-title">Photos</h2>
                        <p className="tools-panel-meta">{plural(workbenchPhotos.length, 'photo')} in view · {selectedVisibleCount} selected</p>
                    </div>
                </div>

                {loading && <Loading label="Loading photos…" fullPage={false} />}
                {!loading && workbenchPhotos.length === 0 && (
                    <EmptyState icon={<PhotoIcon />} title="Nothing to work on" message="No photos match this view right now." />
                )}

                <div className="gallery-grid">
                    {workbenchPhotos.map((photo, index) => {
                        const selectedFlag = selected.has(photo.filename);
                        const tile = (
                            <PhotoTile
                                key={photo.filename}
                                photo={photo}
                                title={photo.filename}
                                selected={selectedFlag}
                                onCardClick={() => toggleOne(photo.filename)}
                                onLongPress={() => setActionSheetTarget({ filenames: [photo.filename], people: photo.people })}
                                selectableOverlay={(
                                    <input
                                        type="checkbox"
                                        aria-label={`Toggle ${photo.filename}`}
                                        checked={selectedFlag}
                                        onClick={(event) => event.stopPropagation()}
                                        onChange={() => toggleOne(photo.filename)}
                                        style={{ touchAction: 'none' }}
                                        {...dragSelectHandlers}
                                    />
                                )}
                                bodyContent={(
                                    <>
                                        {renderPhotoStatusRow(photo, index, true)}
                                        {renderPhotoInfo(photo)}
                                    </>
                                )}
                                mediaOverlay={(
                                    <PhotoQuickActions
                                        libraryHref={libraryFocusHref(photo.filename)}
                                        people={photo.people}
                                    />
                                )}
                            />
                        );
                        if (photo.filename !== pendingFocusFilename) {
                            return tile;
                        }
                        return (
                            <div key={photo.filename} ref={focusTargetRef}>
                                {tile}
                            </div>
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

    const renderRecoveryPage = () => (
        <div className="tools-panel">
            <div className="tools-panel-header">
                <div>
                    <h2 className="tools-panel-title">Recovery actions</h2>
                    <p className="tools-panel-meta">Snapshot-backed repairs for face clustering and search indexes.</p>
                </div>
                <span className="tools-admin-summary-badge">Use sparingly</span>
            </div>
            <div className="tools-recovery-body">
                {lastSnapshot && (
                    <div className="tools-admin-snapshot-card">
                        <span className="tools-admin-snapshot-copy">
                            <strong>Snapshot available</strong>
                            <span>From "{lastSnapshot.label}" — <span className="tools-admin-snapshot-id">{lastSnapshot.snapshotId}</span></span>
                        </span>
                        <button
                            type="button"
                            className="btn btn-soft tools-admin-button"
                            onClick={() => void runRestoreSnapshot()}
                            disabled={!!running}
                        >
                            <ArrowUturnLeftIcon className="toolbar-icon" aria-hidden="true" />
                            <span>{running === 'restoreSnapshot' ? 'Restoring…' : 'Restore'}</span>
                        </button>
                    </div>
                )}
                <button type="button" className="btn btn-soft tools-admin-button" onClick={() => void runBackfillPhotos()} disabled={!!running}>
                    <ArrowPathIcon className="toolbar-icon" aria-hidden="true" />
                    <span className="tools-admin-button-copy">
                        <strong>Backfill all photos</strong>
                        <span>Re-run the full processing pipeline on every photo — thumbnails, EXIF, OCR, AI vision, map tagging, and face detection.</span>
                    </span>
                </button>
                {running === 'backfillPhotos' && <span className="tools-admin-running-badge">Queueing…</span>}

                <div className="tools-admin-section-divider" />
                <div className="tools-admin-callout">
                    <ExclamationTriangleIcon className="tools-admin-callout-icon" />
                    <p>
                        Only use repair actions when clustering or the vector index needs a snapshot-backed reset.
                        Each action asks for confirmation before it runs.
                    </p>
                </div>
                <button type="button" className="btn btn-soft tools-admin-button" onClick={() => void runReclusterPeople()} disabled={!!running}>
                    <UsersIcon className="toolbar-icon" aria-hidden="true" />
                    <span className="tools-admin-button-copy">
                        <strong>Repair people clusters</strong>
                        <span>Snapshots current assignments, then re-clusters detected faces.</span>
                    </span>
                </button>
                <button type="button" className="btn btn-soft tools-admin-button" onClick={() => void runRebuildVectorIndex()} disabled={!!running}>
                    <ArrowPathIcon className="toolbar-icon" aria-hidden="true" />
                    <span className="tools-admin-button-copy">
                        <strong>Rebuild vector index</strong>
                        <span>Refreshes the on-disk index from the latest face embeddings.</span>
                    </span>
                </button>

                <div className="tools-admin-section-divider" />
                <p className="tools-admin-section-label">Preview before applying</p>

                {(['suppressSuspicious', 'dedupeFaces', 'rebuildPeopleIndex', 'repairMemberships', 'unblockFaces', 'purgeOrphaned'] as AdminDryRunPreview['action'][]).map((action) => {
                    const meta: Record<AdminDryRunPreview['action'], { label: string; desc: string; icon: React.ComponentType<React.SVGProps<SVGSVGElement>> }> = {
                        suppressSuspicious: { label: 'Suppress suspicious faces', desc: 'Reject low-confidence faces based on current threshold settings.', icon: EyeIcon },
                        dedupeFaces: { label: 'Deduplicate faces', desc: 'Remove duplicate face rows for the same detected bbox.', icon: UserCircleIcon },
                        rebuildPeopleIndex: { label: 'Rebuild people index', desc: 'Fix photo→person mapping when people are missing from photos.', icon: UsersIcon },
                        repairMemberships: { label: 'Repair stale memberships', desc: 'Fix faces listed in clusters they no longer belong to.', icon: ArrowPathIcon },
                        unblockFaces: { label: 'Unblock low-confidence faces', desc: 'Un-reject faces that now meet the current (lowered) threshold. Run after reducing FACE_LOW_CONFIDENCE_REJECT_BELOW.', icon: BoltIcon },
                        purgeOrphaned: { label: 'Purge orphaned photo data', desc: 'Delete face rows and person records for photos whose image no longer exists in storage. Uses blob storage as the source of truth.', icon: TrashIcon },
                    };
                    const { label, desc, icon: Icon } = meta[action];
                    const isThisRunning = running === action;
                    const preview = dryRunPreview?.action === action ? dryRunPreview.result : null;
                    return (
                        <div key={action} className="tools-admin-dryrun-block">
                            <div className="tools-admin-dryrun-header">
                                <button type="button" className="btn btn-soft tools-admin-button" onClick={() => void runAdminDryRun(action)} disabled={!!running}>
                                    <Icon className="toolbar-icon" aria-hidden="true" />
                                    <span className="tools-admin-button-copy">
                                        <strong>{label}</strong>
                                        <span>{desc}</span>
                                    </span>
                                </button>
                                {isThisRunning && <span className="tools-admin-running-badge">Running…</span>}
                            </div>
                            {preview && (
                                <div className="tools-admin-preview-card">
                                    <p className="tools-admin-preview-title">Preview — no changes applied yet</p>
                                    <ul className="tools-admin-preview-list">
                                        {Object.entries(preview).filter(([k]) => k !== 'success' && k !== 'dryRun' && k !== 'faces' && k !== 'files' && k !== 'orphanedFilenames').map(([k, v]) => {
                                            let display: string;
                                            if (Array.isArray(v)) {
                                                display = v.length === 0 ? '(none)' : v.map((item) => (typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item))).join(', ');
                                            } else if (typeof v === 'object' && v !== null) {
                                                display = JSON.stringify(v);
                                            } else {
                                                display = String(v);
                                            }
                                            return (
                                                <li key={k}><span className="tools-admin-preview-key">{k}:</span> <span className="tools-admin-preview-val">{display}</span></li>
                                            );
                                        })}
                                        {Array.isArray(preview.files) && (
                                            <li><span className="tools-admin-preview-key">files:</span> <span className="tools-admin-preview-val">{(preview.files as unknown[]).length} file(s) affected</span></li>
                                        )}
                                        {Array.isArray(preview.orphanedFilenames) && (
                                            <li><span className="tools-admin-preview-key">orphanedFilenames:</span> <span className="tools-admin-preview-val">{(preview.orphanedFilenames as unknown[]).length === 0 ? '(none)' : `${(preview.orphanedFilenames as unknown[]).length} file(s): ${(preview.orphanedFilenames as string[]).join(', ')}`}</span></li>
                                        )}
                                    </ul>
                                    <button
                                        type="button"
                                        className="btn btn-danger tools-admin-apply-btn"
                                        onClick={() => void runAdminApply(action)}
                                        disabled={!!running}
                                    >
                                        Apply {label}
                                    </button>
                                </div>
                            )}
                        </div>
                    );
                })}

                <Link to="/people" className="btn btn-soft tools-admin-link">
                    <UserCircleIcon className="toolbar-icon" aria-hidden="true" />
                    <span>Review people after a repair</span>
                </Link>
            </div>
        </div>
    );

    return (
        <section className="gallery-wrap card-glass tools-wrap">
            {renderToolsHeader()}
            {renderStatusArea()}
            {isOverviewPage && renderOverviewPage()}
            {isQueueStatusPage && renderQueueStatusPage()}
            {isBrowserWorkbenchPage && renderBrowserWorkbenchPage()}
            {isRecoveryPage && renderRecoveryPage()}

            <PhotoActionSheet
                open={!!actionSheetTarget}
                onClose={() => setActionSheetTarget(null)}
                filenames={actionSheetTarget?.filenames || []}
                people={actionSheetTarget?.people}
                showWorkbenchLink={false}
            />
        </section>
    );
};

export default ToolsPage;
