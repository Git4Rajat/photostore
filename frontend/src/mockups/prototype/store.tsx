import React, { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react';
import { ALBUMS, ALL_SWATCHES, MEMBERS, PEOPLE, PHOTOS, PLACES, SUGGESTIONS, THINGS } from './data';
import type {
    Album,
    Member,
    PageId,
    Person,
    Photo,
    Place,
    Route,
    RouteParams,
    Suggestion,
    ThingTag,
    Toast,
    TrashItem,
} from './types';

interface ViewerState {
    ids: string[];
    index: number;
}

interface Store {
    route: Route;
    photos: Photo[];
    albums: Album[];
    people: Person[];
    members: Member[];
    places: Place[];
    things: ThingTag[];
    suggestions: Suggestion[];
    trash: TrashItem[];
    selection: string[];
    viewer: ViewerState | null;
    toasts: Toast[];
    uploadRequest: number;

    // lookups
    photoById: (id: string) => Photo | undefined;
    photosByIds: (ids: string[]) => Photo[];
    albumById: (id: string) => Album | undefined;
    personById: (id: string) => Person | undefined;

    // navigation
    navigate: (page: PageId, params?: RouteParams) => void;
    requestUpload: () => void;

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
    addPhotos: (count: number) => string[];

    // albums
    createAlbum: (name?: string) => string;
    renameAlbum: (id: string, name: string) => void;
    addPhotosToAlbum: (albumId: string, ids: string[]) => void;
    setAlbumShare: (id: string, patch: Partial<Album['share']>) => void;

    // people
    renamePerson: (id: string, name: string) => void;
    mergePeople: (sourceId: string, targetId: string) => void;

    // members / sharing
    invite: (email: string, role: Member['role']) => void;
    cancelInvite: (id: string) => void;
    resendInvite: (id: string) => void;
    removeMember: (id: string) => void;
    setMemberRole: (id: string, role: Member['role']) => void;

    // toasts
    toast: (message: string, actionLabel?: string, onAction?: () => void) => void;
    dismissToast: (id: string) => void;
}

const StoreContext = createContext<Store | null>(null);

let idSeq = 1000;
const nextId = () => `x${idSeq++}`;
const newCode = () => Math.random().toString(16).slice(2, 6).toUpperCase();

export const StoreProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
    const [route, setRoute] = useState<Route>({ page: 'gallery', params: {} });
    const [photos, setPhotos] = useState<Photo[]>(PHOTOS);
    const [albums, setAlbums] = useState<Album[]>(ALBUMS);
    const [people, setPeople] = useState<Person[]>(PEOPLE);
    const [members, setMembers] = useState<Member[]>(MEMBERS);
    const [trash, setTrash] = useState<TrashItem[]>([]);
    const [selection, setSelection] = useState<string[]>([]);
    const [viewer, setViewer] = useState<ViewerState | null>(null);
    const [toasts, setToasts] = useState<Toast[]>([]);
    const [uploadRequest, setUploadRequest] = useState(0);
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

    const requestUpload = useCallback(() => {
        setRoute({ page: 'gallery', params: {} });
        setUploadRequest((n) => n + 1);
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

    const ratePhotos = useCallback((ids: string[], rating: number) => {
        setPhotos((prev) => prev.map((p) => (ids.includes(p.id) ? { ...p, rating } : p)));
    }, []);

    const toggleLike = useCallback((id: string) => {
        setPhotos((prev) => prev.map((p) => (p.id === id ? { ...p, liked: !p.liked } : p)));
    }, []);

    const removeFromEverywhere = useCallback((ids: string[]) => {
        const set = new Set(ids);
        setPhotos((prev) => prev.filter((p) => !set.has(p.id)));
        setAlbums((prev) => prev.map((a) => ({ ...a, photoIds: a.photoIds.filter((x) => !set.has(x)) })));
        setPeople((prev) => prev.map((p) => ({ ...p, photoIds: p.photoIds.filter((x) => !set.has(x)) })));
    }, []);

    const restorePhotos = useCallback((ids: string[]) => {
        const set = new Set(ids);
        setTrash((prev) => {
            const restored = prev.filter((t) => set.has(t.photo.id)).map((t) => t.photo);
            if (restored.length) setPhotos((cur) => [...restored, ...cur]);
            return prev.filter((t) => !set.has(t.photo.id));
        });
    }, []);

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
        },
        [photosByIds, removeFromEverywhere, toast, restorePhotos],
    );

    const restoreAllTrash = useCallback(() => {
        setTrash((prev) => {
            if (prev.length) setPhotos((cur) => [...prev.map((t) => t.photo), ...cur]);
            return [];
        });
        toast('Restored everything from Recently Deleted');
    }, [toast]);

    const purgePhoto = useCallback((id: string) => {
        setTrash((prev) => prev.filter((t) => t.photo.id !== id));
    }, []);
    const purgeAllTrash = useCallback(() => {
        setTrash([]);
        toast('Recently Deleted emptied');
    }, [toast]);

    const addPhotos = useCallback(
        (count: number) => {
            const created: Photo[] = Array.from({ length: count }, (_, i) => {
                const swatch = ALL_SWATCHES[(idSeq + i) % ALL_SWATCHES.length];
                return {
                    id: nextId(),
                    filename: `IMG_${String(9000 + (idSeq % 900) + i)}.HEIC`,
                    swatch,
                    dateLabel: 'Just now',
                    year: 2026,
                    rating: 0,
                    liked: false,
                    placeId: PLACES[(idSeq + i) % PLACES.length].id,
                    personIds: [],
                    tags: [THINGS[(idSeq + i) % THINGS.length].name.toLowerCase()],
                };
            });
            setPhotos((prev) => [...created, ...prev]);
            return created.map((p) => p.id);
        },
        [],
    );

    const createAlbum = useCallback(
        (name?: string) => {
            const id = nextId();
            const finalName = name ?? 'New album';
            setAlbums((prev) => [
                ...prev,
                { id, name: finalName, coverPhotoId: null, photoIds: [], share: { isPublic: false, expiry: '7', code: newCode() } },
            ]);
            return id;
        },
        [],
    );

    const renameAlbum = useCallback((id: string, name: string) => {
        setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, name } : a)));
    }, []);

    const addPhotosToAlbum = useCallback(
        (albumId: string, ids: string[]) => {
            setAlbums((prev) =>
                prev.map((a) => {
                    if (a.id !== albumId) return a;
                    const merged = Array.from(new Set([...a.photoIds, ...ids]));
                    return { ...a, photoIds: merged, coverPhotoId: a.coverPhotoId ?? merged[0] ?? null };
                }),
            );
            const album = albums.find((a) => a.id === albumId);
            toast(`Added ${ids.length} to “${album?.name ?? 'album'}”`);
        },
        [albums, toast],
    );

    const setAlbumShare = useCallback((id: string, patch: Partial<Album['share']>) => {
        setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, share: { ...a.share, ...patch } } : a)));
    }, []);

    const renamePerson = useCallback((id: string, name: string) => {
        setPeople((prev) => prev.map((p) => (p.id === id ? { ...p, name } : p)));
    }, []);

    const mergePeople = useCallback(
        (sourceId: string, targetId: string) => {
            setPeople((prev) => {
                const source = prev.find((p) => p.id === sourceId);
                const target = prev.find((p) => p.id === targetId);
                if (!source || !target) return prev;
                const mergedIds = Array.from(new Set([...target.photoIds, ...source.photoIds]));
                return prev
                    .filter((p) => p.id !== sourceId)
                    .map((p) => (p.id === targetId ? { ...p, photoIds: mergedIds } : p));
            });
            toast('People merged', 'Undo', () => setPeople(PEOPLE));
        },
        [toast],
    );

    const invite = useCallback(
        (email: string, role: Member['role']) => {
            const initials = email.replace(/@.*/, '').slice(0, 2).toUpperCase() || '?';
            setMembers((prev) => [
                ...prev,
                { id: nextId(), name: email, sub: 'Invited just now', initials, color: '', role, pending: true },
            ]);
            toast(`Invite sent to ${email}`);
        },
        [toast],
    );
    const cancelInvite = useCallback((id: string) => setMembers((prev) => prev.filter((m) => m.id !== id)), []);
    const resendInvite = useCallback((id: string) => {
        const m = members.find((x) => x.id === id);
        toast(`Invite resent${m ? ` to ${m.name}` : ''}`);
    }, [members, toast]);
    const removeMember = useCallback((id: string) => setMembers((prev) => prev.filter((m) => m.id !== id)), []);
    const setMemberRole = useCallback((id: string, role: Member['role']) => {
        setMembers((prev) => prev.map((m) => (m.id === id ? { ...m, role } : m)));
    }, []);

    const value = useMemo<Store>(
        () => ({
            route,
            photos,
            albums,
            people,
            members,
            places: PLACES,
            things: THINGS,
            suggestions: SUGGESTIONS,
            trash,
            selection,
            viewer,
            toasts,
            uploadRequest,
            photoById,
            photosByIds,
            albumById,
            personById,
            navigate,
            requestUpload,
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
            addPhotos,
            createAlbum,
            renameAlbum,
            addPhotosToAlbum,
            setAlbumShare,
            renamePerson,
            mergePeople,
            invite,
            cancelInvite,
            resendInvite,
            removeMember,
            setMemberRole,
            toast,
            dismissToast,
        }),
        [
            route, photos, albums, people, members, trash, selection, viewer, toasts, uploadRequest,
            photoById, photosByIds, albumById, personById, navigate, requestUpload, toggleSelect, selectMany,
            clearSelection, openViewer, closeViewer, viewerStep, ratePhotos, toggleLike, deletePhotos,
            restorePhotos, restoreAllTrash, purgePhoto, purgeAllTrash, addPhotos, createAlbum, renameAlbum,
            addPhotosToAlbum, setAlbumShare, renamePerson, mergePeople, invite, cancelInvite, resendInvite,
            removeMember, setMemberRole, toast, dismissToast,
        ],
    );

    return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
};

export const useStore = (): Store => {
    const ctx = useContext(StoreContext);
    if (!ctx) throw new Error('useStore must be used inside StoreProvider');
    return ctx;
};
