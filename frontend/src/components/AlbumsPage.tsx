import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
    ArrowPathIcon,
    ArrowDownTrayIcon,
    ArrowUpOnSquareIcon,
    CalendarDaysIcon,
    CheckIcon,
    CheckCircleIcon,
    ClockIcon,
    ClipboardIcon,
    EllipsisHorizontalIcon,
    FunnelIcon,
    HeartIcon,
    LinkSlashIcon,
    MagnifyingGlassIcon,
    MapPinIcon,
    MinusCircleIcon,
    PencilSquareIcon,
    PlusIcon,
    RectangleStackIcon,
    SparklesIcon,
    TagIcon,
    TrashIcon,
    UserGroupIcon,
    XMarkIcon,
} from '@heroicons/react/24/outline';
import { HeartIcon as HeartSolidIcon, StarIcon as StarSolidIcon } from '@heroicons/react/24/solid';
import { get, post } from '../services/apiClient';
import { plural } from '../utils/format';
import { confirmDialog, promptDialog } from './shared/dialogs';
import { EmptyState } from './shared/EmptyState';
import { ErrorState } from './shared/ErrorState';
import { Loading } from './shared/Loading';
import PhotoTile from './shared/PhotoTile';
import { useDragSelect } from '../services/useDragSelect';
import PhotoQuickActions, { libraryFocusHref, workbenchFilenameHref } from './shared/PhotoQuickActions';
import PhotoActionSheet from './shared/PhotoActionSheet';
import PhotoViewer from './shared/PhotoViewer';
import { downloadPhotosAsZip } from '../utils/downloadPhotos';
import type { PhotoPersonLink } from '../types/uiTypes';
import { classifyApiError, type ApiError } from '../services/apiError';
import { notifyApiError } from '../services/requestFeedback';
import { useBackendRecoveryRetry } from '../services/useBackendRecoveryRetry';

interface Photo {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    rating?: number;
    likes?: number;
    liked?: boolean;
    tags?: string[];
    rotation?: number;
    location?: { latitude: string; longitude: string; address: string };
    hasExif?: boolean;
    exifSummary?: {
        capturedAt?: string;
        camera?: string;
        lens?: string;
        fNumber?: string;
        exposureTime?: string;
        iso?: string;
        focalLength?: string;
    };
    faceCount?: number;
    people?: PhotoPersonLink[];
}

interface Album {
    id: string;
    name: string;
    photoCount: number;
    filenames?: string[];
    isPublic?: boolean;
    publicUrl?: string;
    publicExpiresAt?: string;
    hasAccessCode?: boolean;
    isExpired?: boolean;
}

type SmartAlbumRule = 'location' | 'recent-upload' | 'person' | 'event-window' | 'tag-object';

const SMART_ALBUM_RULES: Array<{
    id: SmartAlbumRule;
    label: string;
    description: string;
    Icon: React.ComponentType<React.SVGProps<SVGSVGElement>>;
}> = [
    {
        id: 'location',
        label: 'By Location',
        description: 'Places across the library',
        Icon: MapPinIcon,
    },
    {
        id: 'recent-upload',
        label: 'By Recent Upload',
        description: 'Latest upload window',
        Icon: ClockIcon,
    },
    {
        id: 'person',
        label: 'By Person',
        description: 'Matched people clusters',
        Icon: UserGroupIcon,
    },
    {
        id: 'event-window',
        label: 'By Event/Time',
        description: 'Capture date window',
        Icon: CalendarDaysIcon,
    },
    {
        id: 'tag-object',
        label: 'By Tag/Object',
        description: 'AI tags and detected objects',
        Icon: TagIcon,
    },
];

const PAGE_SIZE = 24;

const extractApiErrorMessage = (err: unknown, fallback: string): string => {
    if (typeof err === 'string') {
        return err;
    }
    if (err && typeof err === 'object' && 'error' in err) {
        const message = (err as { error?: unknown }).error;
        if (typeof message === 'string' && message.trim()) {
            return message;
        }
    }
    return fallback;
};

const AlbumsPage: React.FC = () => {
    const location = useLocation();
    const [photos, setPhotos] = useState<Photo[]>([]);
    const [photosLoading, setPhotosLoading] = useState<boolean>(false);
    const [semanticPhotos, setSemanticPhotos] = useState<Photo[] | null>(null);
    const [semanticLoading, setSemanticLoading] = useState<boolean>(false);
    const [albums, setAlbums] = useState<Album[]>([]);
    const [albumsLoading, setAlbumsLoading] = useState<boolean>(false);
    const [activeAlbumId, setActiveAlbumId] = useState<string>('');
    const [activeAlbumPhotos, setActiveAlbumPhotos] = useState<Photo[]>([]);
    const [activeAlbumVisibleCount, setActiveAlbumVisibleCount] = useState<number>(PAGE_SIZE);
    const [albumName, setAlbumName] = useState<string>('');
    const [addAlbumOpen, setAddAlbumOpen] = useState<boolean>(false);
    const [selectedPhotos, setSelectedPhotos] = useState<Set<string>>(new Set());
    const [actionSheetTarget, setActionSheetTarget] = useState<{ filenames: string[]; people?: Photo['people'] } | null>(null);
    const [selectedAlbumIds, setSelectedAlbumIds] = useState<Set<string>>(new Set());
    const [showAddFromGallery, setShowAddFromGallery] = useState<boolean>(false);
    const [searchInput, setSearchInput] = useState<string>('');
    const [searchQuery, setSearchQuery] = useState<string>('');
    const [filterMinRating, setFilterMinRating] = useState<number>(0);
    const [filterLikedOnly, setFilterLikedOnly] = useState<boolean>(false);
    const [searchOpen, setSearchOpen] = useState<boolean>(false);
    const [showFilterMenu, setShowFilterMenu] = useState<boolean>(false);
    const [showActionsMenu, setShowActionsMenu] = useState<boolean>(false);
    const actionsMenuRef = useRef<HTMLDivElement | null>(null);
    const filterMenuRef = useRef<HTMLDivElement | null>(null);
    const searchRef = useRef<HTMLDivElement | null>(null);
    const [lastSharedUrl, setLastSharedUrl] = useState<string>('');
    const [status, setStatus] = useState<string>('');
    const [error, setError] = useState<string>('');
    const [loadError, setLoadError] = useState<ApiError | null>(null);
    const [smartCreateOpen, setSmartCreateOpen] = useState<boolean>(false);
    const [smartCreatingRule, setSmartCreatingRule] = useState<SmartAlbumRule | null>(null);
    const [offset, setOffset] = useState<number>(0);
    const [hasMore, setHasMore] = useState<boolean>(true);
    const [loadingMore, setLoadingMore] = useState<boolean>(false);
    const [downloading, setDownloading] = useState<boolean>(false);
    const [viewerIndex, setViewerIndex] = useState<number | null>(null);

    const observerRef = useRef<IntersectionObserver | null>(null);
    const loadMoreRef = useRef<HTMLDivElement | null>(null);

    const activeAlbum = useMemo(
        () => albums.find((album) => album.id === activeAlbumId) || null,
        [albums, activeAlbumId]
    );

    const updateAlbumList = (albumId: string, updater: (album: Album) => Album) => {
        setAlbums((prev) => prev.map((album) => (album.id === albumId ? updater(album) : album)));
    };

    const removeAlbumsById = (albumIds: string[]) => {
        const removeSet = new Set(albumIds);
        setAlbums((prev) => prev.filter((album) => !removeSet.has(album.id)));
        setSelectedAlbumIds((prev) => {
            const next = new Set(prev);
            albumIds.forEach((albumId) => next.delete(albumId));
            return next;
        });
        if (activeAlbumId && removeSet.has(activeAlbumId)) {
            setActiveAlbumId('');
            setActiveAlbumPhotos([]);
            setActiveAlbumVisibleCount(PAGE_SIZE);
            setShowAddFromGallery(false);
        }
    };

    const syncAlbumFilenames = (filenames: string[], action: 'add' | 'remove') => {
        const fileSet = new Set(filenames);
        const applyChange = (current?: string[]) => {
            const existing = current || [];
            if (action === 'add') {
                return Array.from(new Set([...existing, ...filenames]));
            }
            return existing.filter((filename) => !fileSet.has(filename));
        };

        setAlbums((prev) => prev.map((album) => {
            const nextFilenames = applyChange(album.filenames);
            const delta = action === 'add'
                ? filenames.filter((filename) => !(album.filenames || []).includes(filename)).length
                : -(album.filenames || []).filter((filename) => fileSet.has(filename)).length;
            const nextPhotoCount = Math.max(0, (album.photoCount || 0) + delta);
            return {
                ...album,
                filenames: nextFilenames,
                photoCount: nextPhotoCount,
            };
        }));

        if (activeAlbumId) {
            setActiveAlbumPhotos((prev) => (
                action === 'add'
                    ? prev
                    : prev.filter((photo) => !fileSet.has(photo.filename))
            ));
        }
    };

    const removeDeletedPhotos = (filenames: string[]) => {
        const fileSet = new Set(filenames);
        setPhotos((prev) => prev.filter((photo) => !fileSet.has(photo.filename)));
        setSemanticPhotos((prev) => (prev ? prev.filter((photo) => !fileSet.has(photo.filename)) : prev));
        setActiveAlbumPhotos((prev) => prev.filter((photo) => !fileSet.has(photo.filename)));
        setSelectedPhotos((prev) => {
            const next = new Set(prev);
            filenames.forEach((filename) => next.delete(filename));
            return next;
        });
        setAlbums((prev) => prev.map((album) => {
            const nextFilenames = (album.filenames || []).filter((filename) => !fileSet.has(filename));
            const removedCount = (album.filenames || []).length - nextFilenames.length;
            return {
                ...album,
                filenames: nextFilenames,
                photoCount: Math.max(0, (album.photoCount || 0) - removedCount),
            };
        }));
    };

    const handleSaveRotation = async (filename: string, rotation: number) => {
        // Optimistic: rotate in the UI immediately, roll back if the save fails.
        const previousRotation = [...photos, ...(semanticPhotos || []), ...activeAlbumPhotos]
            .find((photo) => photo.filename === filename)?.rotation ?? 0;
        const applyRotation = (value: number) => (photo: Photo) => (
            photo.filename === filename ? { ...photo, rotation: value } : photo
        );
        const patchAll = (value: number) => {
            setPhotos((prev) => prev.map(applyRotation(value)));
            setSemanticPhotos((prev) => (prev ? prev.map(applyRotation(value)) : prev));
            setActiveAlbumPhotos((prev) => prev.map(applyRotation(value)));
        };
        patchAll(rotation);
        setStatus(`Saved rotation for ${filename}.`);
        try {
            await post(`/photos/${encodeURIComponent(filename)}/rotation`, { rotation });
        } catch (err) {
            patchAll(previousRotation);
            setStatus(`Could not save rotation for ${filename}.`);
        }
    };

    const visiblePhotos = useMemo(() => {
        const semanticSource = semanticPhotos;
        if (!activeAlbumId) {
            return semanticSource || photos;
        }

        if (showAddFromGallery) {
            const albumFilenames = new Set(activeAlbum?.filenames || []);
            return (semanticSource || photos).filter((photo) => !albumFilenames.has(photo.filename));
        }

        if (semanticSource) {
            const albumFilenames = new Set(activeAlbum?.filenames || []);
            return semanticSource.filter((photo) => albumFilenames.has(photo.filename));
        }

        return activeAlbumPhotos.slice(0, activeAlbumVisibleCount);
    }, [photos, semanticPhotos, activeAlbumId, activeAlbum, showAddFromGallery, activeAlbumPhotos, activeAlbumVisibleCount]);

    const filteredPhotos = useMemo(() => {
        const query = searchQuery.trim().toLowerCase();
        return visiblePhotos.filter((photo) => {
            if (filterLikedOnly && !photo.liked) {
                return false;
            }

            if ((photo.rating || 0) < filterMinRating) {
                return false;
            }

            if (!query || semanticPhotos) {
                return true;
            }

            const haystack = [
                photo.filename,
                ...(photo.tags || []),
                photo.location?.address || '',
                photo.location?.latitude || '',
                photo.location?.longitude || '',
                photo.exifSummary?.capturedAt || '',
                photo.exifSummary?.camera || '',
                photo.exifSummary?.lens || '',
            ]
                .join(' ')
                .toLowerCase();

            return haystack.includes(query);
        });
    }, [visiblePhotos, searchQuery, filterLikedOnly, filterMinRating, semanticPhotos]);

    const publicAlbumCount = useMemo(
        () => albums.filter((album) => album.isPublic).length,
        [albums]
    );

    const selectedCount = selectedPhotos.size;

    const fetchPhotosPage = useCallback(async (nextOffset = 0, append = false) => {
        const isInitialLoad = !append && nextOffset === 0;
        if (isInitialLoad) {
            setPhotosLoading(true);
            setError('');
            setLoadError(null);
            setHasMore(true);
        } else {
            setLoadingMore(true);
        }

        try {
            const response = await get(`/photos?sort=date&offset=${nextOffset}&limit=${PAGE_SIZE}`);
            const list = Array.isArray(response?.photos) ? (response.photos as Photo[]) : [];
            const total = typeof response?.total === 'number' ? response.total : nextOffset + list.length;

            setPhotos((prev) => (append ? [...prev, ...list] : list));
            setOffset(nextOffset + list.length);
            setHasMore(list.length === PAGE_SIZE && nextOffset + list.length < total);
        } catch (err) {
            setError('Unable to load photos for albums.');
            if (isInitialLoad) {
                setLoadError(classifyApiError(err));
            }
            setHasMore(false);
        } finally {
            if (isInitialLoad) {
                setPhotosLoading(false);
            } else {
                setLoadingMore(false);
            }
        }
    }, []);

    const fetchSemanticPhotos = useCallback(async (queryText: string) => {
        const trimmedQuery = queryText.trim();
        if (!trimmedQuery) {
            setSemanticPhotos(null);
            setSemanticLoading(false);
            return;
        }
        setSemanticLoading(true);
        setError('');
        try {
            const response = await get(`/photos/search?q=${encodeURIComponent(trimmedQuery)}&offset=0&limit=500`);
            const list = Array.isArray(response?.photos) ? (response.photos as Photo[]) : [];
            setSemanticPhotos(list);
        } catch (err) {
            setSemanticPhotos([]);
            notifyApiError(err, { context: 'Unable to run AI search for albums.', retry: () => { void fetchSemanticPhotos(trimmedQuery); } });
        } finally {
            setSemanticLoading(false);
        }
    }, []);

    const loadAlbums = useCallback(async () => {
        setAlbumsLoading(true);
        try {
            const response = await get('/albums');
            const list = Array.isArray(response?.albums) ? (response.albums as Album[]) : [];
            setAlbums(list);
        } catch (err) {
            setAlbums([]);
            notifyApiError(err, { context: 'Unable to load albums.', retry: () => { void loadAlbums(); } });
        } finally {
            setAlbumsLoading(false);
        }
    }, []);

    const refreshActiveAlbum = useCallback(async () => {
        if (!activeAlbumId) {
            return;
        }
        try {
            const response = await get(`/albums/${activeAlbumId}`);
            if (response?.album) {
                setAlbums((prev) => prev.map((album) => (album.id === activeAlbumId ? response.album : album)));
            }
            const list = Array.isArray(response?.photos) ? (response.photos as Photo[]) : [];
            setActiveAlbumPhotos(list);
            setActiveAlbumVisibleCount(Math.min(PAGE_SIZE, list.length));
        } catch {
            // Ignore refresh errors and keep previous state.
        }
    }, [activeAlbumId]);

    useEffect(() => {
        void loadAlbums();
        void fetchPhotosPage(0, false);
    }, [loadAlbums, fetchPhotosPage]);

    useBackendRecoveryRetry(loadError, () => { void fetchPhotosPage(0, false); });

    useEffect(() => {
        observerRef.current = new IntersectionObserver((entries) => {
            if (!entries[0].isIntersecting || error) {
                return;
            }

            if (activeAlbumId && !showAddFromGallery) {
                if (activeAlbumVisibleCount < activeAlbumPhotos.length) {
                    setActiveAlbumVisibleCount((prev) => Math.min(prev + PAGE_SIZE, activeAlbumPhotos.length));
                }
                return;
            }

            if (searchQuery.trim()) {
                return;
            }

            if (hasMore && !photosLoading && !loadingMore) {
                void fetchPhotosPage(offset, true);
            }
        }, { threshold: 0.1 });

        if (loadMoreRef.current) {
            observerRef.current.observe(loadMoreRef.current);
        }

        return () => {
            if (observerRef.current) {
                observerRef.current.disconnect();
            }
        };
    }, [hasMore, photosLoading, loadingMore, error, offset, fetchPhotosPage, activeAlbumId, showAddFromGallery, activeAlbumVisibleCount, activeAlbumPhotos.length, searchQuery, viewerIndex]);

    const submitSearch = useCallback(() => {
        const nextQuery = searchInput.trim();
        setSearchQuery(nextQuery);
        void fetchSemanticPhotos(nextQuery);
    }, [fetchSemanticPhotos, searchInput]);

    // Leaving the box empty (never submitted, or cleared back out after a prior
    // search) should drop back to the unfiltered gallery rather than leaving a
    // stale query applied — covers click-away, tab-away (blur), and Escape.
    const closeSearch = useCallback(() => {
        if (!searchInput.trim() && searchQuery) {
            setSearchQuery('');
            void fetchSemanticPhotos('');
        }
        setSearchOpen(false);
    }, [searchInput, searchQuery, fetchSemanticPhotos]);

    const clearSearch = useCallback(() => {
        setSearchInput('');
        if (searchQuery) {
            setSearchQuery('');
            void fetchSemanticPhotos('');
        }
        setSearchOpen(false);
    }, [searchQuery, fetchSemanticPhotos]);

    useEffect(() => {
        if (!activeAlbumId) {
            setActiveAlbumPhotos([]);
            setActiveAlbumVisibleCount(PAGE_SIZE);
            return;
        }
        void refreshActiveAlbum();
    }, [activeAlbumId, refreshActiveAlbum]);

    useEffect(() => {
        if (location.pathname !== '/albums') {
            return;
        }
        void fetchPhotosPage(0, false);
        if (activeAlbumId) {
            void refreshActiveAlbum();
        }
    }, [location.pathname, activeAlbumId, fetchPhotosPage, refreshActiveAlbum]);

    useEffect(() => {
        if (!showActionsMenu) {
            return;
        }
        const handlePointerDown = (event: PointerEvent) => {
            if (actionsMenuRef.current && !actionsMenuRef.current.contains(event.target as Node)) {
                setShowActionsMenu(false);
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                setShowActionsMenu(false);
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [showActionsMenu]);

    useEffect(() => {
        if (!showFilterMenu) {
            return;
        }
        const handlePointerDown = (event: PointerEvent) => {
            if (filterMenuRef.current && !filterMenuRef.current.contains(event.target as Node)) {
                setShowFilterMenu(false);
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                setShowFilterMenu(false);
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [showFilterMenu]);

    const selectPhoto = (filename: string) => {
        setSelectedPhotos((prev) => {
            const updated = new Set(prev);
            if (updated.has(filename)) {
                updated.delete(filename);
            } else {
                updated.add(filename);
            }
            return updated;
        });
    };

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

    const handleTileLongPress = (photo: Photo) => {
        if (selectedPhotos.size > 1 && selectedPhotos.has(photo.filename)) {
            setActionSheetTarget({ filenames: Array.from(selectedPhotos) });
        } else {
            setActionSheetTarget({ filenames: [photo.filename], people: photo.people });
        }
    };

    const toggleAlbumSelection = (albumId: string) => {
        setSelectedAlbumIds((prev) => {
            const updated = new Set(prev);
            if (updated.has(albumId)) {
                updated.delete(albumId);
            } else {
                updated.add(albumId);
            }
            return updated;
        });
    };

    const handleDeleteSelectedAlbums = async () => {
        const albumIds = Array.from(selectedAlbumIds);
        if (albumIds.length === 0) return;

        const confirmed = await confirmDialog({
            title: 'Delete albums',
            message: `Delete ${plural(albumIds.length, 'album')}? This cannot be undone.`,
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmed) return;

        setError('');
        setStatus('');
        try {
            const response = await post('/albums/delete-multiple', { albumIds });
            if (response?.success) {
                removeAlbumsById(albumIds);
                setStatus('Selected albums deleted.');
            } else {
                setError('Failed to delete selected albums.');
            }
        } catch (err) {
            notifyApiError(err, { context: 'Failed to delete selected albums.', retry: () => { void handleDeleteSelectedAlbums(); } });
        }
    };

    const handleCreateAlbum = async () => {
        const trimmed = albumName.trim();
        if (!trimmed) {
            return;
        }
        setError('');
        setStatus('');
        try {
            const response = await post('/albums', { name: trimmed });
            if (response?.album) {
                setAlbums((prev) => [response.album as Album, ...prev]);
                setActiveAlbumId(response.album.id);
                setShowAddFromGallery(false);
                setAlbumName('');
                setAddAlbumOpen(false);
                setStatus(`Created album ${trimmed}.`);
            }
        } catch (err) {
            notifyApiError(err, { context: 'Failed to create album.', retry: () => { void handleCreateAlbum(); } });
        }
    };

    const handleAutoCreateAlbums = async (rule: SmartAlbumRule) => {
        setError('');
        setStatus('');
        setSmartCreatingRule(rule);
        try {
            const response = await post('/albums/autocreate', { rule });
            const count = Number(response?.count || 0);
            const created = response?.album as Album | undefined;
            if (count > 0 && created?.id) {
                setAlbums((prev) => (prev.some((album) => album.id === created.id) ? prev : [created, ...prev]));
                setActiveAlbumId(created.id);
                setShowAddFromGallery(false);
                setSelectedPhotos(new Set());
                setSmartCreateOpen(false);
                setStatus(`Created smart album "${created.name}" with ${plural(created.photoCount, 'photo')}.`);
            } else {
                setStatus(response?.message || 'No new matching smart album could be created for this rule.');
            }
        } catch (err) {
            notifyApiError(err, { context: 'Failed to smart create album.', retry: () => { void handleAutoCreateAlbums(rule); } });
        } finally {
            setSmartCreatingRule(null);
        }
    };

    const handleAddSelected = async () => {
        if (!activeAlbumId || selectedPhotos.size === 0) {
            return;
        }
        setError('');
        setStatus('');
        const filenames = Array.from(selectedPhotos);
        const addedPhotos = visiblePhotos.filter((photo) => selectedPhotos.has(photo.filename));
        // Optimistic: reflect the membership change immediately; undo it (the
        // exact reverse operation) if the server rejects the request.
        syncAlbumFilenames(filenames, 'add');
        setActiveAlbumPhotos((prev) => {
            const seen = new Set(prev.map((photo) => photo.filename));
            return [...prev, ...addedPhotos.filter((photo) => !seen.has(photo.filename))];
        });
        setSelectedPhotos(new Set());
        setShowAddFromGallery(false);
        setStatus(`Added ${plural(filenames.length, 'photo')} to album.`);
        try {
            await post(`/albums/${activeAlbumId}/photos/add`, { filenames });
        } catch (err) {
            syncAlbumFilenames(filenames, 'remove');
            const added = new Set(addedPhotos.map((photo) => photo.filename));
            setActiveAlbumPhotos((prev) => prev.filter((photo) => !added.has(photo.filename)));
            setStatus('');
            notifyApiError(err, { context: 'Failed to add photos to album.' });
        }
    };

    const handleRemoveSelected = async () => {
        if (!activeAlbumId || selectedPhotos.size === 0) {
            return;
        }
        setError('');
        setStatus('');
        const filenames = Array.from(selectedPhotos);
        // Optimistic removal with reverse-operation rollback.
        syncAlbumFilenames(filenames, 'remove');
        setSelectedPhotos(new Set());
        setStatus(`Removed ${plural(filenames.length, 'photo')} from album.`);
        try {
            await post(`/albums/${activeAlbumId}/photos/remove`, { filenames });
        } catch (err) {
            syncAlbumFilenames(filenames, 'add');
            setStatus('');
            notifyApiError(err, { context: 'Failed to remove photos from album.' });
        }
    };

    const handleDownloadSelected = async () => {
        if (selectedPhotos.size === 0) {
            return;
        }
        const files = visiblePhotos.filter((photo) => selectedPhotos.has(photo.filename));
        if (files.length === 0) {
            return;
        }

        setDownloading(true);
        setError('');
        setStatus(`Downloading ${plural(files.length, 'photo')}…`);
        try {
            await downloadPhotosAsZip(
                files.map((photo) => ({ filename: photo.filename, url: photo.url })),
                `keepsake-albums-${new Date().toISOString().slice(0, 10)}.zip`
            );
            setStatus(`Downloaded ${plural(files.length, 'photo')}.`);
        } catch (err) {
            setStatus('');
            notifyApiError(err, { context: 'Failed to download selected photos.', retry: () => { void handleDownloadSelected(); } });
        } finally {
            setDownloading(false);
        }
    };

    const handleDeleteSelected = async () => {
        if (selectedPhotos.size === 0) {
            return;
        }

        const deleteCount = selectedPhotos.size;
        const confirmed = await confirmDialog({
            title: 'Delete photos',
            message: `Delete ${plural(deleteCount, 'photo')} from the gallery? This will also remove them from all albums and cannot be undone.`,
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmed) {
            return;
        }

        setError('');
        setStatus('');
        try {
            const response = await post('/photos/delete', { filenames: Array.from(selectedPhotos) });
            const deleted = Array.isArray(response?.deleted) ? (response.deleted as string[]) : [];
            const errorsList = Array.isArray(response?.errors) ? (response.errors as string[]) : [];

            removeDeletedPhotos(deleted);
            setSelectedPhotos(new Set());

            if (errorsList.length > 0 && deleted.length > 0) {
                setStatus(`Deleted ${plural(deleted.length, 'photo')} with ${plural(errorsList.length, 'error')}.`);
                setError(errorsList.join(' • '));
                return;
            }

            if (errorsList.length > 0) {
                setError(errorsList.join(' • '));
                return;
            }

            setStatus(`Deleted ${plural(deleted.length || deleteCount, 'photo')} from gallery and albums.`);
        } catch (err) {
            notifyApiError(err, { context: 'Failed to delete selected photos.', retry: () => { void handleDeleteSelected(); } });
        }
    };


    const handleShareAlbum = async () => {
        if (!activeAlbumId) {
            return;
        }

        const expiresInput = await promptDialog({
            title: 'Share album',
            message: 'How long should the public link stay active? Use 0 for a link that never expires.',
            label: 'Expiry (days)',
            defaultValue: '7',
            confirmLabel: 'Continue',
        });
        if (expiresInput === null) {
            return;
        }

        const expiresInDays = Number.parseInt(expiresInput, 10);
        if (Number.isNaN(expiresInDays) || expiresInDays < 0 || expiresInDays > 365) {
            setError('Expiry must be a number between 0 and 365 days.');
            return;
        }

        const accessCodeInput = await promptDialog({
            title: 'Share album',
            message: 'Optionally protect the link with an access code. Leave blank to share without one.',
            label: 'Access code (optional)',
            defaultValue: '',
            confirmLabel: 'Create link',
        });
        if (accessCodeInput === null) {
            return;
        }

        setError('');
        setStatus('');
        try {
            const response = await post(`/albums/${activeAlbumId}/share`, {
                enabled: true,
                expiresInDays,
                accessCode: accessCodeInput,
                clearAccessCode: accessCodeInput.trim().length === 0,
            });
            const shared = response?.album as Album | undefined;
            if (shared) {
                updateAlbumList(activeAlbumId, () => shared);
            }
            const publicUrl = shared?.publicUrl || '';
            setLastSharedUrl(publicUrl);

            let copied = false;
            if (publicUrl && navigator?.clipboard?.writeText) {
                try {
                    await navigator.clipboard.writeText(publicUrl);
                    copied = true;
                } catch {
                    copied = false;
                }
            }

            if (!publicUrl) {
                setError('Share link was enabled but no URL was returned.');
                return;
            }

            if (shared?.hasAccessCode) {
                setStatus(copied ? 'Secure link copied. Share code separately.' : 'Secure link generated. Copy it below and share code separately.');
            } else {
                setStatus(copied ? 'Public link copied.' : 'Public link generated. Copy it below.');
            }
        } catch (err) {
            notifyApiError(err, { context: 'Failed to create share link.' });
        }
    };

    const handleRevokeLink = async () => {
        if (!activeAlbumId) {
            return;
        }
        const confirmed = await confirmDialog({
            title: 'Revoke link',
            message: 'Revoke this public link now? Anyone who has it will lose access.',
            confirmLabel: 'Revoke',
            danger: true,
        });
        if (!confirmed) {
            return;
        }

        setError('');
        setStatus('');
        try {
            const response = await post(`/albums/${activeAlbumId}/revoke`, {});
            const updated = response?.album as Album | undefined;
            if (updated) {
                updateAlbumList(activeAlbumId, () => updated);
            }
            setLastSharedUrl('');
            setStatus('Public link revoked.');
        } catch (err) {
            notifyApiError(err, { context: 'Failed to revoke public link.', retry: () => { void handleRevokeLink(); } });
        }
    };

        const handleRenameAlbum = async () => {
            if (!activeAlbumId) return;

            const current = activeAlbum?.name || '';
            const input = await promptDialog({
                title: 'Rename album',
                label: 'Album name',
                defaultValue: current,
                confirmLabel: 'Rename',
            });
            if (input === null) return;
            const trimmed = input.trim();
            if (!trimmed || trimmed === current) return;

            setError('');
            setStatus('');
            // Optimistic rename: show the new name immediately, revert on failure.
            updateAlbumList(activeAlbumId, (album) => ({ ...album, name: trimmed }));
            setStatus(`Renamed album to "${trimmed}".`);
            try {
                const response = await post(`/albums/${activeAlbumId}/rename`, { name: trimmed });
                const updated = response?.album as Album | undefined;
                if (updated) {
                    // Adopt the server copy (it may normalize the name).
                    updateAlbumList(activeAlbumId, () => updated);
                }
            } catch (err) {
                updateAlbumList(activeAlbumId, (album) => ({ ...album, name: current }));
                setStatus('');
                notifyApiError(err, { context: 'Failed to rename album.' });
            }
        };

    const handleDeleteAlbum = async () => {
        if (!activeAlbumId) {
            return;
        }

        const confirmed = await confirmDialog({
            title: 'Delete album',
            message: `Delete album "${activeAlbum?.name || ''}"? This cannot be undone.`,
            confirmLabel: 'Delete',
            danger: true,
        });
        if (!confirmed) {
            return;
        }

        setError('');
        setStatus('');
        try {
            const response = await post(`/albums/${activeAlbumId}/delete`, {});
            if (response?.success) {
                removeAlbumsById([activeAlbumId]);
                setStatus('Album deleted.');
            } else {
                setError(extractApiErrorMessage(response, 'Failed to delete album.'));
            }
        } catch (err) {
            notifyApiError(err, { context: 'Failed to delete album.', retry: () => { void handleDeleteAlbum(); } });
        }
    };

    return (
        <section className="gallery-wrap card-glass reveal-up delay-1 albums-page albums-studio">
            <header className="page-topline">
                <h2 className="page-topline-title">Albums</h2>
                <p className="gallery-meta-line" aria-live="polite">
                    <span className="gallery-meta-count">{albums.length}</span>
                    <span> {albums.length === 1 ? 'album' : 'albums'}</span>
                    <span className="gallery-meta-dim"> · {publicAlbumCount} public</span>
                    {selectedCount > 0 && <span className="gallery-meta-dim"> · {selectedCount} selected</span>}
                </p>
            </header>

            <div className="albums-layout-grid">
                <aside className="albums-sidebar">
                    <div className="albums-sidebar-head">
                        <h3 className="toolbar-title">Collection</h3>
                        <button
                            type="button"
                            className="btn btn-soft icon-btn"
                            onClick={() => {
                                setOffset(0);
                                setHasMore(true);
                                void fetchPhotosPage(0, false);
                                void loadAlbums();
                                if (activeAlbumId) {
                                    void refreshActiveAlbum();
                                }
                            }}
                            aria-label="Refresh"
                        >
                            <ArrowPathIcon className="toolbar-icon" />
                            <span className="sr-only">Refresh</span>
                        </button>
                    </div>

                    <div className="albums-actions-row">
                        {addAlbumOpen ? (
                            <div className="gallery-search-open" style={{ flex: 1 }}>
                                <input
                                    type="text"
                                    className="field gallery-search-field"
                                    placeholder="New album name"
                                    value={albumName}
                                    autoFocus
                                    onChange={(e) => setAlbumName(e.target.value)}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter') {
                                            void handleCreateAlbum();
                                        } else if (e.key === 'Escape') {
                                            setAddAlbumOpen(false);
                                        }
                                    }}
                                    onBlur={() => setAddAlbumOpen(false)}
                                    aria-label="New album name"
                                />
                            </div>
                        ) : (
                            <button
                                type="button"
                                className="btn btn-primary icon-btn"
                                onClick={() => setAddAlbumOpen(true)}
                                aria-label="Create album"
                                title="Create album"
                            >
                                <PlusIcon className="toolbar-icon" />
                                <span className="sr-only">Create album</span>
                            </button>
                        )}
                        <button
                            type="button"
                            className={`btn icon-btn ${smartCreateOpen ? 'btn-primary' : 'btn-soft'}`}
                            onClick={() => setSmartCreateOpen((prev) => !prev)}
                            aria-label="Smart create album"
                            title="Smart create album"
                        >
                            <SparklesIcon className="toolbar-icon" />
                            <span className="sr-only">Smart create album</span>
                        </button>
                        {selectedAlbumIds.size > 0 && (
                            <button
                                type="button"
                                className="btn btn-danger icon-btn"
                                onClick={() => void handleDeleteSelectedAlbums()}
                                aria-label={`Delete selected albums (${selectedAlbumIds.size})`}
                                title={`Delete selected albums (${selectedAlbumIds.size})`}
                            >
                                <TrashIcon className="toolbar-icon" />
                                <span className="sr-only">Delete selected albums</span>
                            </button>
                        )}
                    </div>

                    {smartCreateOpen && (
                        <div className="smart-album-picker" role="menu" aria-label="Smart album rules">
                            {SMART_ALBUM_RULES.map(({ id, label, description, Icon }) => {
                                const busy = smartCreatingRule === id;
                                return (
                                    <button
                                        key={id}
                                        type="button"
                                        className="smart-album-rule"
                                        onClick={() => void handleAutoCreateAlbums(id)}
                                        disabled={smartCreatingRule !== null}
                                        role="menuitem"
                                    >
                                        <Icon className="toolbar-icon" />
                                        <span>
                                            <span className="smart-album-rule-label">{busy ? 'Creating…' : label}</span>
                                            <span className="smart-album-rule-meta">{description}</span>
                                        </span>
                                    </button>
                                );
                            })}
                        </div>
                    )}

                    <div className="albums-list">
                        <button
                            type="button"
                            className={`album-list-card ${activeAlbumId === '' ? 'active' : ''}`}
                            onClick={() => {
                                setActiveAlbumId('');
                                setSelectedPhotos(new Set());
                                setShowAddFromGallery(false);
                                setActiveAlbumPhotos([]);
                                setActiveAlbumVisibleCount(PAGE_SIZE);
                            }}
                        >
                            <p className="album-list-name">All Photos</p>
                            <p className="album-list-meta">{photos.length} available</p>
                        </button>

                        {albums.map((album) => (
                            <div key={album.id} style={{ display: 'flex', gap: '8px', alignItems: 'stretch' }}>
                                <button
                                    type="button"
                                    className={`album-list-card ${activeAlbumId === album.id ? 'active' : ''}`}
                            onClick={() => {
                                setActiveAlbumId(album.id);
                                setShowAddFromGallery(false);
                                setSelectedPhotos(new Set());
                            }}
                                    disabled={albumsLoading}
                                    style={{ flex: 1 }}
                                >
                                    <p className="album-list-name">{album.name}</p>
                                    <p className="album-list-meta">
                                        {plural(album.photoCount, 'photo')} 
                                        {album.isPublic ? ' • Public' : ''}
                                        {album.hasAccessCode ? ' • Protected' : ''}
                                    </p>
                                </button>
                                <button
                                    type="button"
                                    className="btn btn-soft icon-btn"
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        toggleAlbumSelection(album.id);
                                    }}
                                    aria-label={selectedAlbumIds.has(album.id) ? 'Deselect album' : 'Select album'}
                                    title={selectedAlbumIds.has(album.id) ? 'Deselect album' : 'Select album'}
                                >
                                    {selectedAlbumIds.has(album.id) ? (
                                        <CheckCircleIcon className="toolbar-icon" style={{ color: 'var(--accent)' }} />
                                    ) : (
                                        <CheckIcon className="toolbar-icon" />
                                    )}
                                </button>
                            </div>
                        ))}

                    </div>
                </aside>

                <section className="albums-canvas">
                    <div className="albums-canvas-toolbar">
                        <div>
                            <h3 className="toolbar-title">{activeAlbum ? activeAlbum.name : 'All Photos'}</h3>
                            <p className="photo-meta">
                                {plural(filteredPhotos.length, 'photo')} 
                                {filteredPhotos.length !== visiblePhotos.length
                                    ? ` (filtered from ${visiblePhotos.length})`
                                    : ''}
                                {activeAlbum
                                    ? (showAddFromGallery ? ' • Gallery photos available to add' : ' • Album photos only')
                                    : ''}
                                {activeAlbum?.publicExpiresAt ? ` • Expires ${activeAlbum.publicExpiresAt}` : ''}
                                {semanticLoading ? ' • AI searching…' : ''}
                            </p>
                        </div>

                        <div className="albums-actions-row" style={{ flexWrap: 'wrap' }}>
                            {searchOpen ? (
                                <div className="gallery-search-open" ref={searchRef}>
                                    <input
                                        type="text"
                                        className="field gallery-search-field"
                                        placeholder="Search by meaning…"
                                        value={searchInput}
                                        autoFocus
                                        onChange={(e) => setSearchInput(e.target.value)}
                                        onKeyDown={(e) => {
                                            if (e.key === 'Enter') {
                                                submitSearch();
                                            } else if (e.key === 'Escape') {
                                                closeSearch();
                                            }
                                        }}
                                        onBlur={closeSearch}
                                        enterKeyHint="search"
                                        aria-label="Search album photos"
                                    />
                                    {searchInput && (
                                        <button
                                            type="button"
                                            className="gallery-search-clear"
                                            onMouseDown={(e) => e.preventDefault()}
                                            onClick={clearSearch}
                                            aria-label="Clear search"
                                        >
                                            <XMarkIcon className="toolbar-icon" />
                                        </button>
                                    )}
                                </div>
                            ) : (
                                <button
                                    type="button"
                                    className={`btn icon-btn ${searchQuery ? 'btn-primary' : 'btn-soft'}`}
                                    onClick={() => setSearchOpen(true)}
                                    aria-label="Search"
                                    title={searchQuery ? `Searching: ${searchQuery}` : 'Search'}
                                >
                                    <MagnifyingGlassIcon className="toolbar-icon" />
                                    <span className="sr-only">Search</span>
                                </button>
                            )}

                            <div className="gallery-menu-anchor" ref={filterMenuRef}>
                                <button
                                    type="button"
                                    className={`btn icon-btn ${showFilterMenu ? 'btn-primary' : (filterMinRating > 0 || filterLikedOnly ? 'btn-primary' : 'btn-soft')}`}
                                    onClick={() => {
                                        setShowActionsMenu(false);
                                        setShowFilterMenu((prev) => !prev);
                                    }}
                                    aria-label="Filters"
                                    aria-expanded={showFilterMenu}
                                    title="Filters"
                                >
                                    <FunnelIcon className="toolbar-icon" />
                                    <span className="sr-only">Filters</span>
                                </button>
                                {showFilterMenu && (
                                    <div className="gallery-menu" role="menu" aria-label="Filters">
                                        <p className="gallery-menu-label">Minimum rating</p>
                                        <div className="gallery-menu-row">
                                            {[0, 1, 2, 3, 4, 5].map((value) => (
                                                <button
                                                    key={value}
                                                    type="button"
                                                    className={`btn btn-soft gallery-menu-btn ${filterMinRating === value ? 'active' : ''}`}
                                                    onClick={() => setFilterMinRating(value)}
                                                >
                                                    {value === 0 ? 'Any' : `${value}+`}
                                                </button>
                                            ))}
                                        </div>
                                        <p className="gallery-menu-label">Likes</p>
                                        <div className="gallery-menu-row">
                                            <button
                                                type="button"
                                                className={`btn btn-soft gallery-menu-btn ${filterLikedOnly ? 'active' : ''}`}
                                                onClick={() => setFilterLikedOnly((prev) => !prev)}
                                            >
                                                <HeartIcon className="toolbar-icon" /> Liked only
                                            </button>
                                        </div>
                                    </div>
                                )}
                            </div>

                            {(searchInput || searchQuery || filterMinRating > 0 || filterLikedOnly) && (
                                <button
                                    type="button"
                                    className="btn btn-soft icon-btn"
                                    onClick={() => {
                                        setSearchInput('');
                                        setSearchQuery('');
                                        setSemanticPhotos(null);
                                        setFilterMinRating(0);
                                        setFilterLikedOnly(false);
                                    }}
                                    aria-label="Clear filters"
                                >
                                    <XMarkIcon className="toolbar-icon" />
                                    <span className="sr-only">Clear filters</span>
                                </button>
                            )}

                            {activeAlbumId && (() => {
                                const addFromGalleryLabel = showAddFromGallery
                                    ? (selectedCount > 0 ? `Add ${selectedCount} to album` : 'Exit add from gallery')
                                    : 'Add from gallery';
                                return (
                                    <button
                                        type="button"
                                        className={`btn icon-btn ${showAddFromGallery ? 'btn-primary' : 'btn-soft'}`}
                                        onClick={() => {
                                            if (showAddFromGallery && selectedCount > 0) {
                                                void handleAddSelected();
                                            } else {
                                                setShowAddFromGallery((prev) => !prev);
                                                setSelectedPhotos(new Set());
                                            }
                                        }}
                                        aria-label={addFromGalleryLabel}
                                        title={addFromGalleryLabel}
                                    >
                                        <PlusIcon className="toolbar-icon" />
                                        <span className="sr-only">{addFromGalleryLabel}</span>
                                    </button>
                                );
                            })()}

                            <div className="gallery-menu-anchor" ref={actionsMenuRef}>
                                <button
                                    type="button"
                                    className={`btn icon-btn ${selectedCount > 0 ? 'btn-primary' : 'btn-soft'}`}
                                    onClick={() => {
                                        setShowFilterMenu(false);
                                        setShowActionsMenu((prev) => !prev);
                                    }}
                                    aria-label="More actions"
                                    aria-expanded={showActionsMenu}
                                    title="More actions"
                                >
                                    <EllipsisHorizontalIcon className="toolbar-icon" />
                                    <span className="sr-only">More actions</span>
                                </button>
                                {showActionsMenu && (
                                    <div className="gallery-menu" role="menu" aria-label="Album actions">
                                        <button
                                            type="button"
                                            className="btn btn-soft gallery-menu-action"
                                            onClick={() => {
                                                if (filteredPhotos.length > 0 && selectedPhotos.size === filteredPhotos.length) {
                                                    setSelectedPhotos(new Set());
                                                } else {
                                                    setSelectedPhotos(new Set(filteredPhotos.map((photo) => photo.filename)));
                                                }
                                            }}
                                        >
                                            <CheckIcon className="toolbar-icon" />
                                            {filteredPhotos.length > 0 && selectedPhotos.size === filteredPhotos.length ? 'Deselect visible' : 'Select visible'}
                                        </button>

                                        {selectedCount > 0 && (
                                            <button type="button" className="btn btn-soft gallery-menu-action" onClick={() => { setSelectedPhotos(new Set()); setShowActionsMenu(false); }}>
                                                <XMarkIcon className="toolbar-icon" />
                                                Clear selection
                                            </button>
                                        )}

                                        {selectedCount > 0 && (
                                            <button type="button" className="btn btn-soft gallery-menu-action" onClick={() => { void handleDownloadSelected(); setShowActionsMenu(false); }} disabled={downloading}>
                                                <ArrowDownTrayIcon className="toolbar-icon" />
                                                Download selected ({selectedCount})
                                            </button>
                                        )}

                                        {activeAlbumId && !showAddFromGallery && selectedCount > 0 && (
                                            <button type="button" className="btn btn-soft gallery-menu-action" onClick={() => { void handleRemoveSelected(); setShowActionsMenu(false); }}>
                                                <MinusCircleIcon className="toolbar-icon" />
                                                Remove from album ({selectedCount})
                                            </button>
                                        )}

                                        {selectedCount > 0 && (
                                            <button type="button" className="btn btn-danger gallery-menu-action" onClick={() => { void handleDeleteSelected(); setShowActionsMenu(false); }}>
                                                <TrashIcon className="toolbar-icon" />
                                                Delete selected ({selectedCount})
                                            </button>
                                        )}

                                        {activeAlbumId && (
                                            <>
                                                <div className="gallery-menu-divider" />
                                                <button type="button" className="btn btn-soft gallery-menu-action" onClick={() => { void handleShareAlbum(); setShowActionsMenu(false); }}>
                                                    <ArrowUpOnSquareIcon className="toolbar-icon" />
                                                    Share album
                                                </button>
                                                {activeAlbum?.isPublic && (
                                                    <button type="button" className="btn btn-danger gallery-menu-action" onClick={() => { void handleRevokeLink(); setShowActionsMenu(false); }}>
                                                        <LinkSlashIcon className="toolbar-icon" />
                                                        Revoke link
                                                    </button>
                                                )}
                                                <button type="button" className="btn btn-soft gallery-menu-action" onClick={() => { void handleRenameAlbum(); setShowActionsMenu(false); }}>
                                                    <PencilSquareIcon className="toolbar-icon" />
                                                    Rename album
                                                </button>
                                                <button type="button" className="btn btn-danger gallery-menu-action" onClick={() => { void handleDeleteAlbum(); setShowActionsMenu(false); }}>
                                                    <TrashIcon className="toolbar-icon" />
                                                    Delete album
                                                </button>
                                            </>
                                        )}
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>

                    {status && <p className="status success">{status}</p>}
                    {error && <p className="status error">{error}</p>}
                    {lastSharedUrl && (
                        <div className="albums-actions-row">
                            <input type="text" className="field" value={lastSharedUrl} readOnly />
                            <button
                                type="button"
                                className="btn btn-soft icon-btn"
                                onClick={async () => {
                                    try {
                                        await navigator.clipboard.writeText(lastSharedUrl);
                                        setStatus('Public link copied.');
                                    } catch {
                                        setError('Unable to copy automatically. You can copy the URL manually.');
                                    }
                                }}
                                aria-label="Copy link"
                            >
                                <ClipboardIcon className="toolbar-icon" />
                                <span className="sr-only">Copy link</span>
                            </button>
                        </div>
                    )}
                    {photosLoading && <Loading label="Loading photos…" fullPage={false} />}

                    {!photosLoading && !activeAlbumId && photos.length === 0 && loadError && (
                        <ErrorState
                            title="Could not load photos"
                            message={loadError.message}
                            onRetry={loadError.retriable ? () => { void fetchPhotosPage(0, false); } : undefined}
                        />
                    )}

                    {!photosLoading && filteredPhotos.length === 0 && !(!activeAlbumId && photos.length === 0 && loadError) && (
                        <EmptyState
                            icon={<RectangleStackIcon />}
                            title={activeAlbum && !showAddFromGallery ? 'This album is empty' : 'Nothing to show here'}
                            message={
                                activeAlbum && !showAddFromGallery
                                    ? 'Use “Add from Gallery” to gather photos into this album.'
                                    : 'No photos available for this view.'
                            }
                        />
                    )}

                    {viewerIndex === null ? (
                        <>
                            <div className="gallery-grid albums-photo-grid">
                                {filteredPhotos.map((photo) => {
                                    const isSelected = selectedPhotos.has(photo.filename);
                                    const rating = Math.max(0, Math.min(5, Math.round(photo.rating || 0)));
                                    return (
                                        <PhotoTile
                                            key={photo.filename}
                                            photo={photo}
                                            selected={isSelected}
                                            title={photo.filename}
                                            showBody={false}
                                            onMediaClick={(e) => {
                                                e.stopPropagation();
                                                e.preventDefault();
                                                setViewerIndex(filteredPhotos.findIndex((item) => item.filename === photo.filename));
                                            }}
                                            onLongPress={() => handleTileLongPress(photo)}
                                            mediaOverlay={(
                                                <>
                                                    <label
                                                        className={`tile-select ${isSelected ? 'is-on' : ''}`}
                                                        onClick={(e) => e.stopPropagation()}
                                                        title={isSelected ? 'Selected' : 'Select photo'}
                                                        {...dragSelectHandlers}
                                                    >
                                                        <input
                                                            type="checkbox"
                                                            className="tile-select-input"
                                                            checked={isSelected}
                                                            onChange={() => selectPhoto(photo.filename)}
                                                            aria-label={`Select ${photo.filename}`}
                                                        />
                                                        <CheckIcon className="tile-select-icon" aria-hidden="true" />
                                                    </label>
                                                    {(rating > 0 || photo.liked) && (
                                                        <div className="tile-badges">
                                                            {rating > 0 && (
                                                                <span className="tile-star" title={`Rated ${rating}/5`} aria-label={`Rated ${rating} out of 5`}>
                                                                    <StarSolidIcon className="tile-star-track" />
                                                                    <span className="tile-star-fill" style={{ width: `${(rating / 5) * 100}%` }}>
                                                                        <StarSolidIcon className="tile-star-front" />
                                                                    </span>
                                                                </span>
                                                            )}
                                                            {photo.liked && (
                                                                <span className="tile-like" title={`${photo.likes || 0} ${photo.likes === 1 ? 'like' : 'likes'}`} aria-label={`Liked, ${photo.likes || 0} likes`}>
                                                                    <HeartSolidIcon className="tile-like-icon" />
                                                                </span>
                                                            )}
                                                        </div>
                                                    )}
                                                    <PhotoQuickActions
                                                        libraryHref={libraryFocusHref(photo.filename)}
                                                        workbenchHref={workbenchFilenameHref(photo.filename)}
                                                        people={photo.people}
                                                    />
                                                </>
                                            )}
                                        />
                                    );
                                })}
                            </div>

                            {((activeAlbumId && !showAddFromGallery && activeAlbumVisibleCount < activeAlbumPhotos.length)
                                || ((showAddFromGallery || !activeAlbumId) && hasMore)) && (
                                <div ref={loadMoreRef} className="load-more-trigger" aria-hidden="true" />
                            )}

                            {loadingMore && (showAddFromGallery || !activeAlbumId) && <p className="status">Loading more photos…</p>}
                        </>
                    ) : (
                        <PhotoViewer
                            photos={filteredPhotos}
                            index={viewerIndex}
                            onClose={() => setViewerIndex(null)}
                            onIndexChange={setViewerIndex}
                            useProtectedMedia={true}
                            onRotationSave={handleSaveRotation}
                        />
                    )}
                </section>
            </div>

            <PhotoActionSheet
                open={!!actionSheetTarget}
                onClose={() => setActionSheetTarget(null)}
                filenames={actionSheetTarget?.filenames || []}
                people={actionSheetTarget?.people}
                onDownload={actionSheetTarget && actionSheetTarget.filenames.length > 1 ? handleDownloadSelected : undefined}
                onDelete={actionSheetTarget && actionSheetTarget.filenames.length > 1 ? handleDeleteSelected : undefined}
                onAlbumsChanged={loadAlbums}
            />
        </section>
    );
};

export default AlbumsPage;
