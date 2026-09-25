import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { SUGGESTIONS } from './data';
import { get, post } from '../../services/apiClient';
import faceService from '../../services/faceService';
import * as library from '../../services/libraryClient';
import type { LibraryMember, PendingInvite } from '../../services/libraryClient';
import type { PersonFace, PersonSummary } from '../../types/people';
import type { Photo as BackendPhoto } from '../../types/uiTypes';
import type {
    Album,
    PageId,
    Person,
    Photo,
    Place,
    Route,
    RouteParams,
    SwatchKey,
    Suggestion,
    ThingTag,
    Toast,
    TrashItem,
} from './types';

// ---- backend <-> prototype photo mapping -------------------------------------

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const PAGE_SIZE = 100;

// Stable placeholder swatch derived from the filename, so a photo shows the same
// tint every render while its real thumbnail loads.
const swatchFor = (filename: string): SwatchKey => {
    let hash = 0;
    for (let i = 0; i < filename.length; i += 1) {
        hash = (hash * 31 + filename.charCodeAt(i)) | 0;
    }
    const n = (Math.abs(hash) % 8) + 1;
    return `s${n}` as SwatchKey;
};

const mapPhoto = (b: BackendPhoto): Photo => {
    const iso = b.captureDate || b.uploadDate || b.lastModified || null;
    const date = iso ? new Date(iso) : null;
    const valid = date && !Number.isNaN(date.getTime()) ? date : null;
    return {
        id: b.filename,
        filename: b.filename,
        swatch: swatchFor(b.filename),
        dateLabel: valid ? `${MONTHS[valid.getMonth()]} ${valid.getDate()}, ${valid.getFullYear()}` : 'Undated',
        year: valid ? valid.getFullYear() : 0,
        rating: b.rating ?? 0,
        liked: Boolean(b.liked),
        likes: b.likes,
        placeId: null,
        personIds: (b.people ?? []).map((p) => p.personId),
        tags: b.tags ?? [],
        thumbnailUrl: b.thumbnailUrl,
        rotation: b.rotation,
        thumbnailRotation: b.thumbnailRotation,
        captureDate: iso,
    };
};

const mapPerson = (p: PersonSummary): Person => {
    const cover = p.representativeFace;
    const coverUrl = cover?.thumbnailUrl
        || (cover?.faceId ? `/api/faces/crop/${encodeURIComponent(cover.faceId)}` : undefined);
    return {
        id: p.personId,
        name: p.name && p.name.trim() ? p.name : null,
        swatch: swatchFor(p.personId),
        photoIds: [],
        coverThumbnailUrl: coverUrl,
        faceCount: p.faceCount,
    };
};

interface ExploreGroup {
    label: string;
    count: number;
    photo?: BackendPhoto;
    latitude?: string;
    longitude?: string;
}

const mapPlace = (g: ExploreGroup): Place => ({
    id: g.label,
    name: g.label,
    swatch: swatchFor(g.label),
    count: g.count,
    coverThumbnailUrl: g.photo?.thumbnailUrl,
    latitude: g.latitude,
    longitude: g.longitude,
});

const mapThing = (g: ExploreGroup): ThingTag => ({
    id: g.label,
    name: g.label,
    count: g.count,
    swatch: swatchFor(g.label),
    coverThumbnailUrl: g.photo?.thumbnailUrl,
});

// Build the set of distinct photos a person appears in, from their face list.
const facesToPhotos = (faces: PersonFace[]): Photo[] => {
    const seen = new Set<string>();
    const out: Photo[] = [];
    for (const face of faces) {
        const filename = face.filename;
        if (!filename || seen.has(filename)) continue;
        seen.add(filename);
        out.push({
            id: filename,
            filename,
            swatch: swatchFor(filename),
            dateLabel: '',
            year: 0,
            rating: 0,
            liked: false,
            placeId: null,
            personIds: [],
            tags: [],
            thumbnailUrl: face.thumbnailUrl,
        });
    }
    return out;
};

interface ViewerState {
    ids: string[];
    index: number;
}

interface Store {
    route: Route;
    photos: Photo[];
    albums: Album[];
    people: Person[];
    members: LibraryMember[];
    pendingInvites: PendingInvite[];
    libraryName: string;
    isOwner: boolean;
    maxMembers: number;
    membersLoading: boolean;
    places: Place[];
    things: ThingTag[];
    suggestions: Suggestion[];
    trash: TrashItem[];
    trashLoading: boolean;
    selection: string[];
    viewer: ViewerState | null;
    toasts: Toast[];

    // photo loading (server-paged)
    photosLoading: boolean;
    hasMorePhotos: boolean;
    loadMorePhotos: () => void;
    reloadPhotos: () => void;

    // explore (places / things from /explore)
    exploreLoading: boolean;
    reloadExplore: () => void;

    // lookups
    photoById: (id: string) => Photo | undefined;
    photosByIds: (ids: string[]) => Photo[];
    albumById: (id: string) => Album | undefined;
    personById: (id: string) => Person | undefined;

    // navigation
    navigate: (page: PageId, params?: RouteParams) => void;

    // selection
    toggleSelect: (id: string) => void;
    selectMany: (ids: string[]) => void;
    clearSelection: () => void;

    // viewer
    openViewer: (ids: string[], index: number) => void;
    closeViewer: () => void;
    viewerStep: (delta: number) => void;

    // photo mutations
    ratePhotos: (ids: string[], rating: number) => void;
    toggleLike: (id: string) => void;
    deletePhotos: (ids: string[]) => void;
    restorePhotos: (ids: string[]) => void;
    restoreAllTrash: () => void;
    purgePhoto: (id: string) => void;
    purgeAllTrash: () => void;
    reloadTrash: () => void;

    // albums (server-backed)
    albumsLoading: boolean;
    reloadAlbums: () => void;
    openAlbum: (id: string) => void;
    albumPhotosById: (id: string) => Photo[] | undefined;
    albumPhotosLoading: boolean;
    createAlbum: (name?: string) => Promise<string>;
    renameAlbum: (id: string, name: string) => void;
    addPhotosToAlbum: (albumId: string, ids: string[]) => void;
    deleteAlbum: (id: string) => void;
    shareAlbum: (id: string, opts: { expiresInDays: number; accessCode?: string }) => Promise<void>;
    revokeAlbum: (id: string) => Promise<void>;

    // people (server-backed)
    peopleLoading: boolean;
    reloadPeople: () => void;
    openPerson: (id: string) => void;
    personPhotosById: (id: string) => Photo[] | undefined;
    personPhotosLoading: boolean;
    renamePerson: (id: string, name: string) => void;
    mergePeople: (sourceId: string, targetId: string) => void;

    // members / sharing (server-backed shared libraries)
    reloadMembers: () => void;
    invite: (email: string, targetType: 'join' | 'fresh') => void;
    revokeInvite: (inviteId: string) => void;
    removeMember: (userId: string) => void;
    renameLibrary: (name: string) => void;

    // toasts
    toast: (message: string, actionLabel?: string, onAction?: () => void) => void;
    dismissToast: (id: string) => void;
}

const StoreContext = createContext<Store | null>(null);

let idSeq = 1000;
const nextId = () => `x${idSeq++}`;

export const StoreProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [route, setRoute] = useState<Route>({ page: 'gallery', params: {} });
    const [photos, setPhotos] = useState<Photo[]>([]);
    const [photosLoading, setPhotosLoading] = useState<boolean>(true);
    const [hasMorePhotos, setHasMorePhotos] = useState<boolean>(true);
    const photoOffsetRef = useRef(0);
    const photoLoadingRef = useRef(false);
    const photoHasMoreRef = useRef(true);
    const [albums, setAlbums] = useState<Album[]>([]);
    const [albumsLoading, setAlbumsLoading] = useState<boolean>(true);
    const [albumPhotos, setAlbumPhotos] = useState<Record<string, Photo[]>>({});
    const [albumPhotosLoading, setAlbumPhotosLoading] = useState<boolean>(false);
    const [placesState, setPlacesState] = useState<Place[]>([]);
    const [thingsState, setThingsState] = useState<ThingTag[]>([]);
    const [exploreLoading, setExploreLoading] = useState<boolean>(true);
    const [people, setPeople] = useState<Person[]>([]);
    const [peopleLoading, setPeopleLoading] = useState<boolean>(true);
    const [personPhotos, setPersonPhotos] = useState<Record<string, Photo[]>>({});
    const [personPhotosLoading, setPersonPhotosLoading] = useState<boolean>(false);
    const [members, setMembers] = useState<LibraryMember[]>([]);
    const [pendingInvites, setPendingInvites] = useState<PendingInvite[]>([]);
    const [libraryName, setLibraryName] = useState<string>('');
    const [isOwner, setIsOwner] = useState<boolean>(false);
    const [maxMembers, setMaxMembers] = useState<number>(15);
    const [membersLoading, setMembersLoading] = useState<boolean>(true);
    const [trash, setTrash] = useState<TrashItem[]>([]);
    const [trashLoading, setTrashLoading] = useState<boolean>(false);
    const [selection, setSelection] = useState<string[]>([]);
    const [viewer, setViewer] = useState<ViewerState | null>(null);
    const [toasts, setToasts] = useState<Toast[]>([]);
    const toastTimers = useRef<Record<string, number>>({});

    const photoIndex = useMemo(() => {
        const m = new Map<string, Photo>();
        for (const p of photos) m.set(p.id, p);
        return m;
    }, [photos]);

    const photoById = useCallback((id: string) => photoIndex.get(id), [photoIndex]);
    const photosByIds = useCallback(
        (ids: string[]) => ids.map((id) => photoIndex.get(id)).filter((p): p is Photo => Boolean(p)),
        [photoIndex],
    );
    const albumById = useCallback((id: string) => albums.find((a) => a.id === id), [albums]);
    const personById = useCallback((id: string) => people.find((p) => p.id === id), [people]);

    // Server-paged photo fetch (mirrors PhotoGallery's /photos?sort=..&offset=..&limit=..).
    const fetchPhotos = useCallback(async (reset: boolean) => {
        if (photoLoadingRef.current) return;
        if (!reset && !photoHasMoreRef.current) return;
        photoLoadingRef.current = true;
        setPhotosLoading(true);
        const offset = reset ? 0 : photoOffsetRef.current;
        try {
            const res = await get<{ photos?: BackendPhoto[] }>(
                `/photos?sort=capture&offset=${offset}&limit=${PAGE_SIZE}`,
            );
            const list = Array.isArray(res?.photos) ? res.photos.map(mapPhoto) : [];
            photoOffsetRef.current = offset + list.length;
            photoHasMoreRef.current = list.length === PAGE_SIZE;
            setHasMorePhotos(photoHasMoreRef.current);
            setPhotos((prev) => (reset ? list : [...prev, ...list]));
        } catch {
            photoHasMoreRef.current = false;
            setHasMorePhotos(false);
        } finally {
            photoLoadingRef.current = false;
            setPhotosLoading(false);
        }
    }, []);

    useEffect(() => {
        void fetchPhotos(true);
    }, [fetchPhotos]);

    const loadMorePhotos = useCallback(() => { void fetchPhotos(false); }, [fetchPhotos]);
    const reloadPhotos = useCallback(() => {
        photoOffsetRef.current = 0;
        photoHasMoreRef.current = true;
        void fetchPhotos(true);
    }, [fetchPhotos]);

    const dismissToast = useCallback((id: string) => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
        const timer = toastTimers.current[id];
        if (timer) {
            window.clearTimeout(timer);
            delete toastTimers.current[id];
        }
    }, []);

    const toast = useCallback(
        (message: string, actionLabel?: string, onAction?: () => void) => {
            const id = nextId();
            setToasts((prev) => [...prev, { id, message, actionLabel, onAction }]);
            toastTimers.current[id] = window.setTimeout(() => {
                setToasts((prev) => prev.filter((t) => t.id !== id));
                delete toastTimers.current[id];
            }, 4200);
        },
        [],
    );

    const navigate = useCallback((page: PageId, params: RouteParams = {}) => {
        setRoute({ page, params });
        setSelection([]);
        setViewer(null);
        window.scrollTo(0, 0);
    }, []);

    const toggleSelect = useCallback((id: string) => {
        setSelection((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    }, []);
    const selectMany = useCallback((ids: string[]) => setSelection(ids), []);
    const clearSelection = useCallback(() => setSelection([]), []);

    const openViewer = useCallback((ids: string[], index: number) => setViewer({ ids, index }), []);
    const closeViewer = useCallback(() => setViewer(null), []);
    const viewerStep = useCallback((delta: number) => {
        setViewer((prev) => {
            if (!prev) return prev;
            const index = Math.min(prev.ids.length - 1, Math.max(0, prev.index + delta));
            return { ...prev, index };
        });
    }, []);

    const ratePhotos = useCallback(
        (ids: string[], rating: number) => {
            if (!ids.length) return;
            const set = new Set(ids);
            const prevRatings = new Map(photos.filter((p) => set.has(p.id)).map((p) => [p.id, p.rating]));
            setPhotos((prev) => prev.map((p) => (set.has(p.id) ? { ...p, rating } : p)));
            void post('/photos/rate-multiple', { filenames: ids, rating }).catch(() => {
                setPhotos((prev) => prev.map((p) => (prevRatings.has(p.id) ? { ...p, rating: prevRatings.get(p.id)! } : p)));
                toast('Couldn’t save rating');
            });
        },
        [photos, toast],
    );

    const toggleLike = useCallback(
        (id: string) => {
            const current = photos.find((p) => p.id === id);
            const nextLiked = !current?.liked;
            const prevLikes = current?.likes ?? 0;
            const optimisticLikes = Math.max(0, prevLikes + (nextLiked ? 1 : -1));
            setPhotos((prev) => prev.map((p) => (p.id === id ? { ...p, liked: nextLiked, likes: optimisticLikes } : p)));
            void post<{ liked?: boolean; likes?: number }>(`/photos/${encodeURIComponent(id)}/like`, {})
                .then((res) => {
                    setPhotos((prev) => prev.map((p) => (p.id === id
                        ? { ...p, liked: res.liked ?? nextLiked, likes: res.likes ?? optimisticLikes }
                        : p)));
                })
                .catch(() => {
                    setPhotos((prev) => prev.map((p) => (p.id === id
                        ? { ...p, liked: Boolean(current?.liked), likes: prevLikes }
                        : p)));
                    toast('Couldn’t update like');
                });
        },
        [photos, toast],
    );

    const removeFromEverywhere = useCallback((ids: string[]) => {
        const set = new Set(ids);
        setPhotos((prev) => prev.filter((p) => !set.has(p.id)));
        // Albums are server-managed (deletion cascades server-side); just prune
        // the client-side album-photos cache so open albums reflect the removal.
        setAlbumPhotos((prev) => {
            const next: Record<string, Photo[]> = {};
            for (const [albumId, list] of Object.entries(prev)) {
                next[albumId] = list.filter((p) => !set.has(p.id));
            }
            return next;
        });
        setPersonPhotos((prev) => {
            const next: Record<string, Photo[]> = {};
            for (const [personId, list] of Object.entries(prev)) {
                next[personId] = list.filter((p) => !set.has(p.id));
            }
            return next;
        });
    }, []);

    const restorePhotos = useCallback((ids: string[]) => {
        if (!ids.length) return;
        const set = new Set(ids);
        // Optimistically move the photos back from the local trash cache; roll
        // back if the server rejects the restore.
        let moved: Photo[] = [];
        setTrash((prev) => {
            moved = prev.filter((t) => set.has(t.photo.id)).map((t) => t.photo);
            if (moved.length) setPhotos((cur) => [...moved, ...cur]);
            return prev.filter((t) => !set.has(t.photo.id));
        });
        void post('/photos/trash/restore', { filenames: ids }).catch(() => {
            const movedSet = new Set(moved.map((p) => p.id));
            setPhotos((cur) => cur.filter((p) => !movedSet.has(p.id)));
            setTrash((prev) => [...moved.map((photo) => ({ photo, purgesInDays: 30 })), ...prev]);
            toast('Couldn’t restore photos');
        });
    }, [toast]);

    const deletePhotos = useCallback(
        (ids: string[]) => {
            if (!ids.length) return;
            const doomed = photosByIds(ids);
            setTrash((prev) => [...doomed.map((photo) => ({ photo, purgesInDays: 30 })), ...prev]);
            removeFromEverywhere(ids);
            setSelection([]);
            toast(
                `Deleted ${ids.length} photo${ids.length > 1 ? 's' : ''}`,
                'Undo',
                () => restorePhotos(ids),
            );
            void post('/photos/delete', { filenames: ids }).catch(() => {
                // Roll back the optimistic removal on failure.
                const set = new Set(ids);
                setTrash((prev) => prev.filter((t) => !set.has(t.photo.id)));
                setPhotos((cur) => [...doomed, ...cur]);
                toast('Couldn’t delete photos');
            });
        },
        [photosByIds, removeFromEverywhere, toast, restorePhotos],
    );

    const reloadTrash = useCallback(async () => {
        setTrashLoading(true);
        try {
            const res = await get<{ photos?: (BackendPhoto & { purgeAt?: string })[] }>('/photos/trash?limit=200');
            const now = Date.now();
            const items: TrashItem[] = Array.isArray(res?.photos)
                ? res.photos.map((p) => {
                    const purgeAt = (p as { purgeAt?: string }).purgeAt;
                    const days = purgeAt ? Math.max(0, Math.ceil((new Date(purgeAt).getTime() - now) / 86400000)) : 30;
                    return { photo: mapPhoto(p), purgesInDays: days };
                })
                : [];
            setTrash(items);
        } catch {
            // keep the current list on failure
        } finally {
            setTrashLoading(false);
        }
    }, []);

    const restoreAllTrash = useCallback(() => {
        const snapshot = trash;
        setTrash([]);
        setPhotos((cur) => [...snapshot.map((t) => t.photo), ...cur]);
        void post('/photos/trash/restore-all', {})
            .then(() => toast('Restored everything from Recently Deleted'))
            .catch(() => {
                setTrash(snapshot);
                toast('Couldn’t restore everything');
            });
    }, [trash, toast]);

    const purgePhoto = useCallback((id: string) => {
        const snapshot = trash;
        setTrash((prev) => prev.filter((t) => t.photo.id !== id));
        void post('/photos/trash/purge', { filenames: [id] }).catch(() => {
            setTrash(snapshot);
            toast('Couldn’t delete photo');
        });
    }, [trash, toast]);

    const purgeAllTrash = useCallback(() => {
        const snapshot = trash;
        const filenames = snapshot.map((t) => t.photo.id);
        if (!filenames.length) return;
        setTrash([]);
        void post('/photos/trash/purge', { filenames })
            .then(() => toast('Recently Deleted emptied'))
            .catch(() => {
                setTrash(snapshot);
                toast('Couldn’t empty Recently Deleted');
            });
    }, [trash, toast]);

    const fetchAlbums = useCallback(async () => {
        setAlbumsLoading(true);
        try {
            const res = await get<{ albums?: Album[] }>('/albums');
            setAlbums(Array.isArray(res?.albums) ? res.albums : []);
        } catch {
            // leave the current list in place on transient failures
        } finally {
            setAlbumsLoading(false);
        }
    }, []);

    useEffect(() => {
        void fetchAlbums();
    }, [fetchAlbums]);

    const reloadAlbums = useCallback(() => { void fetchAlbums(); }, [fetchAlbums]);

    const fetchExplore = useCallback(async () => {
        setExploreLoading(true);
        try {
            const data = await get<{ places?: ExploreGroup[]; things?: ExploreGroup[] }>('/explore');
            setPlacesState(Array.isArray(data?.places) ? data.places.map(mapPlace) : []);
            setThingsState(Array.isArray(data?.things) ? data.things.map(mapThing) : []);
        } catch {
            // leave whatever we have on transient failures
        } finally {
            setExploreLoading(false);
        }
    }, []);

    useEffect(() => {
        void fetchExplore();
    }, [fetchExplore]);

    const reloadExplore = useCallback(() => { void fetchExplore(); }, [fetchExplore]);

    const openAlbum = useCallback(async (id: string) => {
        setAlbumPhotosLoading(true);
        try {
            const res = await get<{ album?: Album; photos?: BackendPhoto[] }>(`/albums/${encodeURIComponent(id)}`);
            const list = Array.isArray(res?.photos) ? res.photos.map(mapPhoto) : [];
            setAlbumPhotos((prev) => ({ ...prev, [id]: list }));
            if (res?.album) {
                setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, ...res.album } : a)));
            }
        } catch {
            setAlbumPhotos((prev) => ({ ...prev, [id]: prev[id] ?? [] }));
        } finally {
            setAlbumPhotosLoading(false);
        }
    }, []);

    const albumPhotosById = useCallback((id: string) => albumPhotos[id], [albumPhotos]);

    const createAlbum = useCallback(
        async (name?: string): Promise<string> => {
            const finalName = (name ?? 'New album').trim() || 'New album';
            try {
                const res = await post<{ album?: Album }>('/albums', { name: finalName });
                const created = res?.album;
                if (created) {
                    setAlbums((prev) => [...prev, created]);
                    return created.id;
                }
            } catch {
                toast('Couldn’t create album');
            }
            return '';
        },
        [toast],
    );

    const renameAlbum = useCallback((id: string, name: string) => {
        const trimmed = name.trim();
        if (!trimmed) return;
        const previous = albums.find((a) => a.id === id)?.name;
        setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, name: trimmed } : a)));
        void post(`/albums/${encodeURIComponent(id)}/rename`, { name: trimmed }).catch(() => {
            setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, name: previous ?? a.name } : a)));
            toast('Couldn’t rename album');
        });
    }, [albums, toast]);

    const addPhotosToAlbum = useCallback(
        (albumId: string, ids: string[]) => {
            if (!ids.length) return;
            const album = albums.find((a) => a.id === albumId);
            setAlbums((prev) => prev.map((a) => (a.id === albumId ? { ...a, photoCount: a.photoCount + ids.length } : a)));
            void post(`/albums/${encodeURIComponent(albumId)}/photos/add`, { filenames: ids })
                .then(() => {
                    toast(`Added ${ids.length} to “${album?.name ?? 'album'}”`);
                    // Refresh the album's photos if it's currently open/cached.
                    if (albumPhotos[albumId]) void openAlbum(albumId);
                })
                .catch(() => {
                    setAlbums((prev) => prev.map((a) => (a.id === albumId ? { ...a, photoCount: Math.max(0, a.photoCount - ids.length) } : a)));
                    toast('Couldn’t add photos to album');
                });
        },
        [albums, albumPhotos, openAlbum, toast],
    );

    const deleteAlbum = useCallback((id: string) => {
        const removed = albums.find((a) => a.id === id);
        setAlbums((prev) => prev.filter((a) => a.id !== id));
        void post('/albums/delete-multiple', { albumIds: [id] })
            .then(() => toast(`Deleted “${removed?.name ?? 'album'}”`))
            .catch(() => {
                if (removed) setAlbums((prev) => [...prev, removed]);
                toast('Couldn’t delete album');
            });
    }, [albums, toast]);

    const shareAlbum = useCallback(async (id: string, opts: { expiresInDays: number; accessCode?: string }) => {
        try {
            const res = await post<{ album?: Album }>(`/albums/${encodeURIComponent(id)}/share`, {
                enabled: true,
                expiresInDays: opts.expiresInDays,
                accessCode: opts.accessCode ?? '',
                clearAccessCode: !opts.accessCode,
            });
            if (res?.album) {
                setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, ...res.album } : a)));
            }
        } catch {
            toast('Couldn’t update share link');
        }
    }, [toast]);

    const revokeAlbum = useCallback(async (id: string) => {
        try {
            const res = await post<{ album?: Album }>(`/albums/${encodeURIComponent(id)}/revoke`, {});
            setAlbums((prev) => prev.map((a) => (a.id === id
                ? { ...a, ...(res?.album ?? {}), isPublic: false, publicUrl: undefined }
                : a)));
        } catch {
            toast('Couldn’t revoke share link');
        }
    }, [toast]);

    const fetchPeople = useCallback(async () => {
        setPeopleLoading(true);
        try {
            const res = await faceService.listPersons(undefined, 0, 200);
            const list = Array.isArray(res?.persons) ? (res.persons as PersonSummary[]).map(mapPerson) : [];
            setPeople(list);
        } catch {
            // keep the current list on transient failures
        } finally {
            setPeopleLoading(false);
        }
    }, []);

    useEffect(() => {
        void fetchPeople();
    }, [fetchPeople]);

    const reloadPeople = useCallback(() => { void fetchPeople(); }, [fetchPeople]);

    const openPerson = useCallback(async (id: string) => {
        setPersonPhotosLoading(true);
        try {
            const res = await faceService.getPerson(id) as { faces?: PersonFace[]; name?: string; faceCount?: number };
            const faces = Array.isArray(res?.faces) ? res.faces : [];
            setPersonPhotos((prev) => ({ ...prev, [id]: facesToPhotos(faces) }));
        } catch {
            setPersonPhotos((prev) => ({ ...prev, [id]: prev[id] ?? [] }));
        } finally {
            setPersonPhotosLoading(false);
        }
    }, []);

    const personPhotosById = useCallback((id: string) => personPhotos[id], [personPhotos]);

    const renamePerson = useCallback((id: string, name: string) => {
        const trimmed = name.trim();
        const previous = people.find((p) => p.id === id)?.name ?? null;
        setPeople((prev) => prev.map((p) => (p.id === id ? { ...p, name: trimmed || null } : p)));
        void faceService.labelPerson(id, trimmed).catch(() => {
            setPeople((prev) => prev.map((p) => (p.id === id ? { ...p, name: previous } : p)));
            toast('Couldn’t save name');
        });
    }, [people, toast]);

    const mergePeople = useCallback(
        (sourceId: string, targetId: string) => {
            // Optimistically drop the source cluster; the target absorbs it.
            const removed = people.find((p) => p.id === sourceId);
            setPeople((prev) => prev.filter((p) => p.id !== sourceId));
            void faceService.mergePersons(targetId, [sourceId])
                .then(() => {
                    toast('People merged');
                    void fetchPeople();
                })
                .catch(() => {
                    if (removed) setPeople((prev) => [...prev, removed]);
                    toast('Couldn’t merge people');
                });
        },
        [people, fetchPeople, toast],
    );

    const fetchMembers = useCallback(async () => {
        setMembersLoading(true);
        try {
            const res = await library.getMembers();
            setMembers(res.members ?? []);
            setPendingInvites(res.pendingInvites ?? []);
            setLibraryName(res.name ?? '');
            setIsOwner(Boolean(res.isOwner));
            setMaxMembers(res.maxMembers ?? 15);
        } catch {
            // keep whatever we have on transient failures
        } finally {
            setMembersLoading(false);
        }
    }, []);

    useEffect(() => {
        void fetchMembers();
    }, [fetchMembers]);

    const reloadMembers = useCallback(() => { void fetchMembers(); }, [fetchMembers]);

    const invite = useCallback((email: string, targetType: 'join' | 'fresh') => {
        void library.sendInvite(email, targetType)
            .then(() => { toast(`Invite sent to ${email}`); void fetchMembers(); })
            .catch((err) => toast(err instanceof Error ? err.message : 'Couldn’t send invite'));
    }, [fetchMembers, toast]);

    const revokeInvite = useCallback((inviteId: string) => {
        setPendingInvites((prev) => prev.filter((p) => p.inviteId !== inviteId));
        void library.revokePendingInvite(inviteId)
            .then(() => toast('Invitation revoked'))
            .catch(() => { toast('Couldn’t revoke invite'); void fetchMembers(); });
    }, [fetchMembers, toast]);

    const removeMember = useCallback((userId: string) => {
        setMembers((prev) => prev.filter((m) => m.userId !== userId));
        void library.removeMember(userId)
            .then(() => toast('Member removed'))
            .catch(() => { toast('Couldn’t remove member'); void fetchMembers(); });
    }, [fetchMembers, toast]);

    const renameLibrary = useCallback((name: string) => {
        const trimmed = name.trim();
        if (!trimmed) return;
        const previous = libraryName;
        setLibraryName(trimmed);
        void library.renameLibrary(trimmed)
            .then(() => toast('Library renamed'))
            .catch(() => { setLibraryName(previous); toast('Couldn’t rename library'); });
    }, [libraryName, toast]);

    const value = useMemo<Store>(
        () => ({
            route,
            photos,
            albums,
            people,
            members,
            pendingInvites,
            libraryName,
            isOwner,
            maxMembers,
            membersLoading,
            places: placesState,
            things: thingsState,
            suggestions: SUGGESTIONS,
            trash,
            trashLoading,
            selection,
            viewer,
            toasts,
            photosLoading,
            hasMorePhotos,
            loadMorePhotos,
            reloadPhotos,
            exploreLoading,
            reloadExplore,
            photoById,
            photosByIds,
            albumById,
            personById,
            navigate,
            toggleSelect,
            selectMany,
            clearSelection,
            openViewer,
            closeViewer,
            viewerStep,
            ratePhotos,
            toggleLike,
            deletePhotos,
            restorePhotos,
            restoreAllTrash,
            purgePhoto,
            purgeAllTrash,
            reloadTrash,
            albumsLoading,
            reloadAlbums,
            openAlbum,
            albumPhotosById,
            albumPhotosLoading,
            createAlbum,
            renameAlbum,
            addPhotosToAlbum,
            deleteAlbum,
            shareAlbum,
            revokeAlbum,
            peopleLoading,
            reloadPeople,
            openPerson,
            personPhotosById,
            personPhotosLoading,
            renamePerson,
            mergePeople,
            reloadMembers,
            invite,
            revokeInvite,
            removeMember,
            renameLibrary,
            toast,
            dismissToast,
        }),
        [
            route, photos, albums, people, members, pendingInvites, libraryName, isOwner, maxMembers, membersLoading,
            placesState, thingsState, trash, trashLoading, selection, viewer, toasts,
            photosLoading, hasMorePhotos, loadMorePhotos, reloadPhotos, exploreLoading, reloadExplore,
            photoById, photosByIds, albumById, personById, navigate, toggleSelect, selectMany,
            clearSelection, openViewer, closeViewer, viewerStep, ratePhotos, toggleLike, deletePhotos,
            restorePhotos, restoreAllTrash, purgePhoto, purgeAllTrash, reloadTrash,
            albumsLoading, reloadAlbums, openAlbum, albumPhotosById, albumPhotosLoading,
            createAlbum, renameAlbum, addPhotosToAlbum, deleteAlbum, shareAlbum, revokeAlbum,
            peopleLoading, reloadPeople, openPerson, personPhotosById, personPhotosLoading,
            renamePerson, mergePeople, reloadMembers, invite, revokeInvite,
            removeMember, renameLibrary, toast, dismissToast,
        ],
    );

    return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
};

export const useStore = (): Store => {
    const ctx = useContext(StoreContext);
    if (!ctx) throw new Error('useStore must be used inside StoreProvider');
    return ctx;
};
