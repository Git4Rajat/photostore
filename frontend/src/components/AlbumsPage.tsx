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
    HeartIcon,
    LinkSlashIcon,
    MapPinIcon,
    MinusCircleIcon,
    PencilSquareIcon,
    PlusIcon,
    SparklesIcon,
    TagIcon,
    TrashIcon,
    UserGroupIcon,
    XMarkIcon,
} from '@heroicons/react/24/outline';
import { get, post } from '../services/apiClient';
import { getMediaKind } from '../utils/photoDisplay';
import MetricCard from './shared/MetricCard';
import PhotoTile from './shared/PhotoTile';
import PhotoViewer from './shared/PhotoViewer';
import { downloadPhotosAsZip } from '../utils/downloadPhotos';

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
    const [selectedPhotos, setSelectedPhotos] = useState<Set<string>>(new Set());
    const [selectedAlbumIds, setSelectedAlbumIds] = useState<Set<string>>(new Set());
    const [showAddFromGallery, setShowAddFromGallery] = useState<boolean>(false);
    const [searchInput, setSearchInput] = useState<string>('');
    const [searchQuery, setSearchQuery] = useState<string>('');
    const [filterMinRating, setFilterMinRating] = useState<number>(0);
    const [filterLikedOnly, setFilterLikedOnly] = useState<boolean>(false);
    const [lastSharedUrl, setLastSharedUrl] = useState<string>('');
    const [status, setStatus] = useState<string>('');
    const [error, setError] = useState<string>('');
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
        await post(`/photos/${encodeURIComponent(filename)}/rotation`, { rotation });
        const applyRotation = (photo: Photo) => (
            photo.filename === filename ? { ...photo, rotation } : photo
        );
        setPhotos((prev) => prev.map(applyRotation));
        setSemanticPhotos((prev) => (prev ? prev.map(applyRotation) : prev));
        setActiveAlbumPhotos((prev) => prev.map(applyRotation));
        setStatus(`Saved rotation for ${filename}.`);
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

    const formatCaptureDate = (capturedAt?: string): string => {
        if (!capturedAt) {
            return '';
        }
        const normalized = capturedAt.replace(/^(\d{4}):(\d{2}):(\d{2})/, '$1-$2-$3').replace(' ', 'T');
        const parsed = new Date(normalized);
        if (Number.isNaN(parsed.getTime())) {
            return capturedAt;
        }
        return parsed.toLocaleDateString(undefined, {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
        });
    };

    const fetchPhotosPage = useCallback(async (nextOffset = 0, append = false) => {
        const isInitialLoad = !append && nextOffset === 0;
        if (isInitialLoad) {
            setPhotosLoading(true);
            setError('');
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
        } catch {
            setError('Unable to load photos for albums.');
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
        } catch {
            setSemanticPhotos([]);
            setError('Unable to run AI search for albums.');
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
        } catch {
            setAlbums([]);
            setError('Unable to load albums.');
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
    }, [hasMore, photosLoading, loadingMore, error, offset, fetchPhotosPage, activeAlbumId, showAddFromGallery, activeAlbumVisibleCount, activeAlbumPhotos.length, searchQuery]);

    const submitSearch = useCallback(() => {
        const nextQuery = searchInput.trim();
        setSearchQuery(nextQuery);
        void fetchSemanticPhotos(nextQuery);
    }, [fetchSemanticPhotos, searchInput]);

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

        const confirmed = window.confirm(
            `Delete ${albumIds.length} album(s)? This cannot be undone.`
        );
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
            setError(extractApiErrorMessage(err, 'Failed to delete selected albums.'));
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
                setStatus(`Created album ${trimmed}.`);
            }
        } catch (err) {
            setError(extractApiErrorMessage(err, 'Failed to create album.'));
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
                setStatus(`Created smart album "${created.name}" with ${created.photoCount} photo(s).`);
            } else {
                setStatus(response?.message || 'No new matching smart album could be created for this rule.');
            }
        } catch (err) {
            setError(extractApiErrorMessage(err, 'Failed to smart create album.'));
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
        try {
            await post(`/albums/${activeAlbumId}/photos/add`, { filenames });
            syncAlbumFilenames(filenames, 'add');
            setActiveAlbumPhotos((prev) => {
                const seen = new Set(prev.map((photo) => photo.filename));
                return [...prev, ...addedPhotos.filter((photo) => !seen.has(photo.filename))];
            });
            setSelectedPhotos(new Set());
            setShowAddFromGallery(false);
            setStatus(`Added ${selectedPhotos.size} photo(s) to album.`);
        } catch {
            setError('Failed to add photos to album.');
        }
    };

    const handleRemoveSelected = async () => {
        if (!activeAlbumId || selectedPhotos.size === 0) {
            return;
        }
        setError('');
        setStatus('');
        const filenames = Array.from(selectedPhotos);
        try {
            await post(`/albums/${activeAlbumId}/photos/remove`, { filenames });
            syncAlbumFilenames(filenames, 'remove');
            setSelectedPhotos(new Set());
            setStatus(`Removed ${selectedPhotos.size} photo(s) from album.`);
        } catch {
            setError('Failed to remove photos from album.');
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
        setStatus(`Downloading ${files.length} photo(s)...`);
        try {
            await downloadPhotosAsZip(
                files.map((photo) => ({ filename: photo.filename, url: photo.url })),
                `photostore-albums-${new Date().toISOString().slice(0, 10)}.zip`
            );
            setStatus(`Downloaded ${files.length} photo(s).`);
        } catch (err) {
            setError(typeof err === 'string' ? err : 'Failed to download selected photos.');
        } finally {
            setDownloading(false);
        }
    };

    const handleDeleteSelected = async () => {
        if (selectedPhotos.size === 0) {
            return;
        }

        const deleteCount = selectedPhotos.size;
        const confirmed = window.confirm(
            `Delete ${deleteCount} photo(s) from the gallery? This will also remove them from all albums and cannot be undone.`
        );
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
                setStatus(`Deleted ${deleted.length} photo(s) with ${errorsList.length} error(s).`);
                setError(errorsList.join(' • '));
                return;
            }

            if (errorsList.length > 0) {
                setError(errorsList.join(' • '));
                return;
            }

            setStatus(`Deleted ${deleted.length || deleteCount} photo(s) from gallery and albums.`);
        } catch {
            setError('Failed to delete selected photos.');
        }
    };


    const handleShareAlbum = async () => {
        if (!activeAlbumId) {
            return;
        }

        const expiresInput = window.prompt('Public link expiry in days (0 for no expiry):', '7');
        if (expiresInput === null) {
            return;
        }

        const expiresInDays = Number.parseInt(expiresInput, 10);
        if (Number.isNaN(expiresInDays) || expiresInDays < 0 || expiresInDays > 365) {
            setError('Expiry must be a number between 0 and 365 days.');
            return;
        }

        const accessCodeInput = window.prompt('Optional access code (leave blank for no code):', '');
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
        } catch {
            setError('Failed to create share link.');
        }
    };

    const handleRevokeLink = async () => {
        if (!activeAlbumId) {
            return;
        }
        const confirmed = window.confirm('Revoke this public link now?');
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
        } catch {
            setError('Failed to revoke public link.');
        }
    };

        const handleRenameAlbum = async () => {
            if (!activeAlbumId) return;

            const current = activeAlbum?.name || '';
            const input = window.prompt('Rename album:', current);
            if (input === null) return;
            const trimmed = input.trim();
            if (!trimmed || trimmed === current) return;

            setError('');
            setStatus('');
            try {
                const response = await post(`/albums/${activeAlbumId}/rename`, { name: trimmed });
                const updated = response?.album as Album | undefined;
                if (updated) {
                    updateAlbumList(activeAlbumId, () => updated);
                    setStatus(`Renamed album to "${trimmed}".`);
                } else {
                    setError('Rename succeeded but no album returned.');
                }
            } catch (err) {
                setError(extractApiErrorMessage(err, 'Failed to rename album.'));
            }
        };

    const handleDeleteAlbum = async () => {
        if (!activeAlbumId) {
            return;
        }

        const confirmed = window.confirm(
            `Delete album "${activeAlbum?.name || ''}"? This cannot be undone.`
        );
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
            setError(extractApiErrorMessage(err, 'Failed to delete album.'));
        }
    };

    return (
        <section className="gallery-wrap card-glass reveal-up delay-1 albums-page albums-studio">
            <div className="albums-banner">
                <div className="albums-banner-copy">
                    <p className="additional-kicker">CURATION WORKSPACE</p>
                    <h2 className="albums-banner-title">Albums</h2>
                    <p className="photo-meta">Compose and share narrative photo sets with precise control.</p>
                </div>
                <div className="albums-metrics">
                    <MetricCard value={albums.length} label="Total Albums" />
                    <MetricCard value={publicAlbumCount} label="Public Links" />
                    <MetricCard value={selectedCount} label="Selected Photos" />
                </div>
            </div>

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
                        <input
                            type="text"
                            className="field"
                            placeholder="New album name"
                            value={albumName}
                            onChange={(e) => setAlbumName(e.target.value)}
                        />
                        <button type="button" className="btn btn-primary icon-btn" onClick={handleCreateAlbum} aria-label="Create album">
                            <PlusIcon className="toolbar-icon" />
                            <span className="sr-only">Create album</span>
                        </button>
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
                                            <span className="smart-album-rule-label">{busy ? 'Creating...' : label}</span>
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
                                        {album.photoCount} photo(s)
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
                                        <CheckCircleIcon className="toolbar-icon" style={{ color: 'var(--accent-color, #4f46e5)' }} />
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
                                {filteredPhotos.length} photo(s)
                                {filteredPhotos.length !== visiblePhotos.length
                                    ? ` (filtered from ${visiblePhotos.length})`
                                    : ''}
                                {activeAlbum
                                    ? (showAddFromGallery ? ' • Gallery photos available to add' : ' • Album photos only')
                                    : ''}
                                {activeAlbum?.publicExpiresAt ? ` • Expires ${activeAlbum.publicExpiresAt}` : ''}
                                {semanticLoading ? ' • AI searching...' : ''}
                            </p>
                        </div>

                        <div className="albums-actions-row" style={{ flexWrap: 'wrap', overflowX: 'auto' }}>
                            <input
                                type="text"
                                className="field"
                                placeholder="AI Search"
                                value={searchInput}
                                onChange={(e) => setSearchInput(e.target.value)}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter') {
                                        submitSearch();
                                    }
                                }}
                                enterKeyHint="search"
                            />

                            <select
                                className="field"
                                value={String(filterMinRating)}
                                onChange={(e) => setFilterMinRating(Number(e.target.value))}
                            >
                                <option value="0">Min rating: Any</option>
                                <option value="1">Min rating: 1+</option>
                                <option value="2">Min rating: 2+</option>
                                <option value="3">Min rating: 3+</option>
                                <option value="4">Min rating: 4+</option>
                                <option value="5">Min rating: 5</option>
                            </select>

                            <button
                                type="button"
                                className={`btn icon-btn ${filterLikedOnly ? 'btn-primary' : 'btn-soft'}`}
                                onClick={() => setFilterLikedOnly((prev) => !prev)}
                                aria-label={filterLikedOnly ? 'Liked only on' : 'Liked only'}
                            >
                                <HeartIcon className="toolbar-icon" />
                                <span className="sr-only">{filterLikedOnly ? 'Liked only on' : 'Liked only'}</span>
                            </button>

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

                            {activeAlbumId && (
                                <button
                                    type="button"
                                    className={`btn icon-btn ${showAddFromGallery ? 'btn-primary' : 'btn-soft'}`}
                                    onClick={() => {
                                        setShowAddFromGallery((prev) => !prev);
                                        setSelectedPhotos(new Set());
                                    }}
                                    aria-label={showAddFromGallery ? 'Add mode gallery photos' : 'Add from gallery'}
                                >
                                    <PlusIcon className="toolbar-icon" />
                                    <span className="sr-only">{showAddFromGallery ? 'Add mode gallery photos' : 'Add from gallery'}</span>
                                </button>
                            )}

                            <button
                                type="button"
                                className="btn btn-soft icon-btn"
                                onClick={() => {
                                    if (filteredPhotos.length > 0 && selectedPhotos.size === filteredPhotos.length) {
                                        setSelectedPhotos(new Set());
                                    } else {
                                        setSelectedPhotos(new Set(filteredPhotos.map((photo) => photo.filename)));
                                    }
                                }}
                                aria-label={filteredPhotos.length > 0 && selectedPhotos.size === filteredPhotos.length ? 'Deselect visible' : 'Select visible'}
                            >
                                <CheckIcon className="toolbar-icon" />
                                <span className="sr-only">
                                    {filteredPhotos.length > 0 && selectedPhotos.size === filteredPhotos.length
                                        ? 'Deselect visible'
                                        : 'Select visible'}
                                </span>
                            </button>

                            {selectedCount > 0 && (
                                <button
                                    type="button"
                                    className="btn btn-soft icon-btn"
                                    onClick={() => setSelectedPhotos(new Set())}
                                    aria-label="Clear selection"
                                >
                                    <XMarkIcon className="toolbar-icon" />
                                    <span className="sr-only">Clear selection</span>
                                </button>
                            )}

                            {selectedCount > 0 && (
                                <button
                                    type="button"
                                    className="btn btn-soft icon-btn"
                                    onClick={handleDownloadSelected}
                                    disabled={downloading}
                                    aria-label={`Download selected (${selectedCount})`}
                                >
                                    <ArrowDownTrayIcon className="toolbar-icon" />
                                    <span className="sr-only">Download selected ({selectedCount})</span>
                                </button>
                            )}

                            {selectedAlbumIds.size > 0 && (
                                <button type="button" className="btn btn-danger icon-btn" onClick={handleDeleteSelectedAlbums} aria-label={`Delete selected albums (${selectedAlbumIds.size})`}>
                                    <TrashIcon className="toolbar-icon" />
                                    <span className="sr-only">Delete selected albums ({selectedAlbumIds.size})</span>
                                </button>
                            )}

                            {activeAlbumId && (
                                <>
                                    <button type="button" className="btn btn-soft icon-btn" onClick={handleShareAlbum} aria-label="Share album" title="Share album">
                                        <ArrowUpOnSquareIcon className="toolbar-icon" />
                                        <span className="sr-only">Share album</span>
                                    </button>
                                    {activeAlbum?.isPublic && (
                                        <button type="button" className="btn btn-danger icon-btn" onClick={handleRevokeLink} aria-label="Revoke link" title="Revoke public link">
                                            <LinkSlashIcon className="toolbar-icon" />
                                            <span className="sr-only">Revoke link</span>
                                        </button>
                                    )}
                                    <button type="button" className="btn btn-soft icon-btn" onClick={handleRenameAlbum} aria-label="Rename album" title="Rename album">
                                        <PencilSquareIcon className="toolbar-icon" />
                                        <span className="sr-only">Rename album</span>
                                    </button>
                                    <button type="button" className="btn btn-danger icon-btn" onClick={handleDeleteAlbum} aria-label="Delete album" title="Delete album">
                                        <TrashIcon className="toolbar-icon" />
                                        <span className="sr-only">Delete album</span>
                                    </button>
                                </>
                            )}

                            {activeAlbumId && showAddFromGallery && selectedCount > 0 && (
                                <>
                                    <button type="button" className="btn btn-primary icon-btn" onClick={handleAddSelected} aria-label={`Add ${selectedCount}`}>
                                        <PlusIcon className="toolbar-icon" />
                                        <span className="sr-only">Add {selectedCount}</span>
                                    </button>
                                </>
                            )}

                            {activeAlbumId && !showAddFromGallery && selectedCount > 0 && (
                                <>
                                    <button type="button" className="btn btn-soft icon-btn" onClick={handleRemoveSelected} aria-label={`Remove ${selectedCount}`}>
                                        <MinusCircleIcon className="toolbar-icon" />
                                        <span className="sr-only">Remove {selectedCount}</span>
                                    </button>
                                    <button type="button" className="btn btn-danger icon-btn" onClick={handleDeleteSelected} aria-label={`Delete ${selectedCount}`}>
                                        <TrashIcon className="toolbar-icon" />
                                        <span className="sr-only">Delete {selectedCount}</span>
                                    </button>
                                </>
                            )}
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
                    {photosLoading && <p className="status">Loading photos...</p>}

                    {!photosLoading && filteredPhotos.length === 0 && (
                        <p className="empty">
                            {activeAlbum && !showAddFromGallery
                                ? 'No photos in this album yet. Use Add from Gallery to include more photos.'
                                : 'No photos available for this view.'}
                        </p>
                    )}

                    <div className="gallery-grid albums-photo-grid">
                        {filteredPhotos.map((photo) => (
                            <PhotoTile
                                key={photo.filename}
                                photo={photo}
                                selected={selectedPhotos.has(photo.filename)}
                                title={photo.filename}
                                kind={getMediaKind(photo.filename)}
                                openOriginal={false}
                                onCardClick={() => selectPhoto(photo.filename)}
                                onMediaClick={(e) => {
                                    e.stopPropagation();
                                    e.preventDefault();
                                    setViewerIndex(filteredPhotos.findIndex((item) => item.filename === photo.filename));
                                }}
                                bodyContent={(
                                    <>
                                        <div className="photo-row">
                                            <div className="rating-display">
                                                {photo.rating ? '★'.repeat(photo.rating) : '☆'} {photo.rating || 0}/5
                                            </div>
                                            <div
                                                className={`like-btn ${photo.liked ? 'active' : ''}`}
                                                title={`${photo.likes || 0} likes`}
                                                aria-label={`${photo.likes || 0} likes`}
                                            >
                                                <HeartIcon className="toolbar-icon" />
                                                <span className="sr-only">{photo.likes || 0} likes</span>
                                            </div>
                                        </div>
                                        {photo.exifSummary?.capturedAt && (
                                            <p className="photo-location">Captured: {formatCaptureDate(photo.exifSummary.capturedAt)}</p>
                                        )}
                                    </>
                                )}
                            />
                        ))}
                    </div>

                    {((activeAlbumId && !showAddFromGallery && activeAlbumVisibleCount < activeAlbumPhotos.length)
                        || ((showAddFromGallery || !activeAlbumId) && hasMore)) && (
                        <div ref={loadMoreRef} className="load-more-trigger" aria-hidden="true" />
                    )}

                    {loadingMore && (showAddFromGallery || !activeAlbumId) && <p className="status">Loading more photos...</p>}
                    {viewerIndex === null ? null : (
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
        </section>
    );
};

export default AlbumsPage;
