import { onIndexReady, reportIndexBuilding } from '../../services/indexBuilding';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { SUGGESTIONS } from './data';
import { get, post } from '../../services/apiClient';
import { getLocalSortIndex, invalidateLocalSortIndex, isServerPagedLibrary, patchLocalSortIndexRow, removeLocalSortIndexRows, type SortIndexRow } from '../../services/localSortIndex';
import { getCachedMediaToken, getMediaToken, thumbnailUrlForBlob, type MediaToken } from '../../services/mediaToken';
import { getLocalAlbumsIndex, invalidateLocalAlbumsIndex } from '../../services/localAlbumsIndex';
import { invalidateLocalPeopleIndex } from '../../services/localPeopleIndex';
import { resolveThumbnailAccessUrls } from '../../services/thumbnailAccessCache';
import { enqueueBackgroundRequest } from '../../services/backgroundRequestQueue';
import { requestJobPoll } from '../../services/jobNotifications';
import { publishLibraryChange } from '../../services/libraryChanges';
import { chunk, measureGridCapacity, pageSizeForCapacity } from '../../services/gridCapacity';
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
    AlbumTrashItem,
} from './types';

// ---- backend <-> prototype photo mapping -------------------------------------

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const PAGE_SIZE = 100; // legacy /photos fallback page size
// lookup-batch accepts <=200 filenames; enrich in parallel chunks of this size.
const ENRICH_CHUNK = 100;

// The sort/albums/people local indexes all report "unavailable" immediately
// on a cold account instead of blocking (their backend routes kick a
// background rebuild and return right away) -- see the 2026-09-29 forenkla-qa
// HAR investigation. Each caller below used to treat that first null as
// final and fall straight through to its O(library size) legacy endpoint,
// which is exactly the multi-second-to-minutes cost the index exists to
// avoid. A short bounded retry gives the just-kicked-off rebuild a chance to
// land before paying that cost -- and still falls back for a genuinely
// unavailable index (network error, feature not supported) after 3 tries.
const withIndexRetry = async <T,>(fetchIndex: () => Promise<T | null>): Promise<T | null> => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
        const result = await fetchIndex();
        if (result) return result;
        if (isServerPagedLibrary()) return null;   // too big for a client index: retrying cannot help
        if (attempt < 2) await new Promise((resolve) => setTimeout(resolve, 3000));
    }
    return null;
};

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

export const mapPhoto = (b: BackendPhoto, token: MediaToken | null = getCachedMediaToken()): Photo => {
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
        // Token mode: the backend sends a blob name and the browser builds the
        // direct URL; otherwise (or for proxy fallbacks) the backend's URL.
        thumbnailUrl: (b.thumbnailBlob && token ? thumbnailUrlForBlob(b.thumbnailBlob, token) : '') || b.thumbnailUrl,
        rotation: b.rotation,
        thumbnailRotation: b.thumbnailRotation,
        captureDate: iso,
        processing: b.processing,
    };
};

// Backend-free first paint: a Photo built from nothing but a sort-index row and
// the media token. Metadata the index doesn't carry (people, tags, per-user
// "liked", processing status, thumbnail rotation) arrives with the background
// lookup-batch enrichment and replaces this entry by id.
const provisionalPhoto = (row: SortIndexRow, token: MediaToken | null): Photo => {
    const iso = row.captureDate || row.uploadDate || null;
    const date = iso ? new Date(iso) : null;
    const valid = date && !Number.isNaN(date.getTime()) ? date : null;
    return {
        id: row.filename,
        filename: row.filename,
        swatch: swatchFor(row.filename),
        dateLabel: valid ? `${MONTHS[valid.getMonth()]} ${valid.getDate()}, ${valid.getFullYear()}` : 'Undated',
        year: valid ? valid.getFullYear() : 0,
        rating: row.rating,
        liked: false,
        likes: row.likes,
        placeId: null,
        personIds: [],
        tags: [],
        thumbnailUrl: thumbnailUrlForBlob(row.thumb, token) || undefined,
        captureDate: iso,
    };
};

const rowCaptureTime = (row: SortIndexRow): number => {
    const raw = row.captureDate ? new Date(row.captureDate).getTime() : 0;
    return Number.isFinite(raw) ? raw : 0;
};

const filterRowsForGallery = (rows: SortIndexRow[], filters: GalleryFilters): SortIndexRow[] => (
    rows.filter((row) => {
        if (filters.rating > 0 && (Number(row.rating) || 0) !== filters.rating) return false;
        if (filters.likedOnly && (Number(row.likes) || 0) <= 0) return false;
        return true;
    })
);

const sortRowsForGallery = (rows: SortIndexRow[]): SortIndexRow[] => (
    [...rows].sort((a, b) => {
        const at = rowCaptureTime(a);
        const bt = rowCaptureTime(b);
        if (at !== bt) return bt - at;
        return a.filename < b.filename ? -1 : a.filename > b.filename ? 1 : 0;
    })
);

interface PeoplePageRow { personId: string; name: string; isNamed: boolean; faceCount: number; coverFaceId?: string }
interface PeoplePageResponse {
    available?: boolean; rows?: PeoplePageRow[]; total?: number; hasMore?: boolean;
    namedCount?: number; unnamedCount?: number;
}
const PEOPLE_PAGE = 120;
const mapPersonRow = (r: PeoplePageRow): Person => ({
    id: r.personId,
    name: r.isNamed && (r.name ?? '').trim() ? r.name : null,
    swatch: swatchFor(r.personId),
    photoIds: [],
    // The avatar loader turns this into a direct storage URL (face-crop token); the path is only the
    // fallback that generates a crop that doesn't exist yet.
    coverThumbnailUrl: r.coverFaceId ? `/api/faces/crop/${encodeURIComponent(r.coverFaceId)}` : undefined,
    faceCount: r.faceCount,
});

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

interface ViewerState {
    ids: string[];
    index: number;
    // When true, the viewer is backed by the gallery's infinite list: its ids
    // grow as more pages load, and nearing the end triggers a fetch. Viewers
    // opened from a fully-loaded set (albums, a person, search) leave this off.
    extendable?: boolean;
}

export type MediaFilter = 'all' | 'photo' | 'video';

export interface GalleryFilters {
    rating: number;
    likedOnly: boolean;
}

export interface CaptureRange {
    // Inclusive ISO date bounds (yyyy-mm-dd) passed to /photos as
    // captureStart/captureEnd; either end may be omitted for an open range.
    start?: string;
    end?: string;
    label: string;
}

// Shape of GET /photos/timeline (see backend build_timeline_summary): a nested
// year -> month -> day count tree the gallery's timeline rail is built from.
export interface TimelineSummary {
    years: Record<string, {
        count: number;
        coverFilename?: string;
        coverThumbnailUrl?: string;
        months: Record<string, {
            count: number;
            days: Record<string, number>;
            coverFilename?: string;
            coverThumbnailUrl?: string;
        }>;
    }>;
    firstDate: string | null;
    lastDate: string | null;
    undatedCount: number;
    totalCount: number;
}

const VIDEO_EXT_RE = /\.(mp4|mov|m4v|webm|avi|mkv|3gp|hevc)$/i;
export const isVideoFilename = (filename: string): boolean => VIDEO_EXT_RE.test(filename);

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
    albumTrash: AlbumTrashItem[];
    albumTrashLoading: boolean;
    selection: string[];
    viewer: ViewerState | null;
    toasts: Toast[];

    // photo loading (server-paged)
    photosLoading: boolean;
    hasMorePhotos: boolean;
    totalPhotos: number | null;
    loadMorePhotos: () => void;
    reloadPhotos: () => void;
    applyExternalPhotoDeleteCount: (count: number) => void;

    // gallery filters / timeline
    mediaFilter: MediaFilter;
    setMediaFilter: (filter: MediaFilter) => void;
    galleryFilters: GalleryFilters;
    setGalleryRating: (rating: number) => void;
    setGalleryLikedOnly: (likedOnly: boolean) => void;
    jumpToGalleryDate: (date: string) => Promise<string | null>;
    captureRange: CaptureRange | null;
    setCaptureRange: (range: CaptureRange | null) => void;
    timeline: TimelineSummary | null;

    // explore (places / things from /explore)
    exploreLoading: boolean;
    reloadExplore: () => void;
    // Promise-returning counterpart of reloadExplore, for callers (the
    // Explore tab's own mount effect) that need to queue/await/cancel it
    // instead of firing it unconditionally -- see that page's mount effect.
    fetchExplore: () => Promise<void>;

    // lookups
    photoById: (id: string) => Photo | undefined;
    photosByIds: (ids: string[]) => Photo[];
    albumById: (id: string) => Album | undefined;
    personById: (id: string) => Person | undefined;
    // Merges photos from a source outside the paginated gallery list (e.g.
    // /photos/search results) into the shared lookup so the viewer can find
    // them by id. See photoIndex below.
    registerPhotos: (list: Photo[]) => void;

    // navigation
    navigate: (page: PageId, params?: RouteParams) => void;

    // selection
    toggleSelect: (id: string) => void;
    selectMany: (ids: string[]) => void;
    clearSelection: () => void;
    // iOS-style explicit selection mode: photo grids only show checkboxes and
    // treat a tap as "toggle selection" while this is on, so scrolling can't
    // trigger accidental selections. Reset on navigate / clear.
    selectMode: boolean;
    setSelectMode: (on: boolean) => void;

    // viewer
    openViewer: (ids: string[], index: number, opts?: { extendable?: boolean }) => void;
    closeViewer: () => void;
    viewerStep: (delta: number) => void;
    // Open a single photo in the viewer within the gallery sequence, resolving
    // it by exact filename lookup when it isn't on a loaded gallery page yet.
    focusPhoto: (filename: string) => void;

    // photo mutations
    ratePhotos: (ids: string[], rating: number) => void;
    toggleLike: (id: string) => void;
    applyPhotoRotation: (filename: string, rotation: number) => void;
    deletePhotos: (ids: string[]) => void;
    restorePhotos: (ids: string[]) => void;
    restoreAllTrash: () => void;
    purgePhoto: (id: string) => void;
    purgeAllTrash: () => void;
    reloadTrash: () => void;
    reloadAlbumTrash: () => void;
    restoreAlbum: (id: string) => void;
    purgeAlbum: (id: string) => void;

    // albums (server-backed)
    albumsLoading: boolean;
    reloadAlbums: () => void;
    // Promise-returning counterpart of reloadAlbums -- see fetchExplore above.
    fetchAlbums: () => Promise<void>;
    openAlbum: (id: string) => void;
    albumPhotosById: (id: string) => Photo[] | undefined;
    isAlbumPhotosLoading: (id: string) => boolean;
    loadMoreAlbumPhotos: (id: string) => void;
    albumPhotosTotal: (id: string) => number | undefined;
    albumPhotosHasMore: (id: string) => boolean;
    createAlbum: (name?: string) => Promise<string>;
    autoCreateAlbum: (rule: string) => Promise<{ albumId: string; count: number; message?: string }>;
    renameAlbum: (id: string, name: string) => void;
    addPhotosToAlbum: (albumId: string, ids: string[]) => void;
    deleteAlbum: (id: string) => void;
    deleteAlbums: (ids: string[]) => void;
    shareAlbum: (id: string, opts: { expiresInDays: number; accessCode?: string }) => Promise<void>;
    revokeAlbum: (id: string) => Promise<void>;

    // people (server-backed)
    peopleLoading: boolean;
    /** Clusters in the library / how many still need a name (the list below loads a screenful at a time). */
    peopleTotal: number;
    peopleUnnamedTotal: number;
    peopleHasMore: boolean;
    /** The server's paged people list could not be loaded (index still building, or a network error). */
    peopleUnavailable: boolean;
    loadMorePeople: () => void;
    /** Server-side name search (not stored in the list). */
    searchPeople: (query: string, limit?: number) => Promise<Person[]>;
    reloadPeople: () => void;
    // Promise-returning counterpart of reloadPeople -- see fetchExplore above.
    fetchPeople: () => Promise<void>;
    openPerson: (id: string) => void;
    personPhotosById: (id: string) => Photo[] | undefined;
    /** Total photos of the person on the server (the list below fills in a screenful at a time). */
    personPhotosTotal: (id: string) => number | undefined;
    personPhotosHasMore: (id: string) => boolean;
    loadMorePersonPhotos: (id: string) => void;
    personPhotosLoading: boolean;
    renamePerson: (id: string, name: string) => void;
    mergePeople: (sourceId: string, targetId: string) => void;
    mergePeopleBatch: (targetId: string, sourceIds: string[]) => void;
    deletePerson: (id: string) => void;
    deletePeopleBatch: (ids: string[]) => void;
    applyExternalPeopleRemoval: (ids: string[]) => void;

    // members / sharing (server-backed shared libraries)
    reloadMembers: () => void;
    // Promise-returning counterpart of reloadMembers -- see fetchExplore above.
    fetchMembers: () => Promise<void>;
    invite: (email: string, targetType: 'join' | 'fresh') => void;
    revokeInvite: (inviteId: string) => void;
    removeMember: (userId: string) => void;
    renameLibrary: (name: string) => void;

    // toasts
    toast: (message: string, actionLabel?: string, onAction?: () => void, tone?: 'info' | 'error') => void;
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
    const [totalPhotos, setTotalPhotos] = useState<number | null>(null);
    // A delete is authoritative before the derived gallery index catches up.
    // Never let a stale refresh raise the visible count above this ceiling.
    const photoCountCeilingRef = useRef<number | null>(null);
    const [mediaFilter, setMediaFilterState] = useState<MediaFilter>('all');
    const [galleryFilters, setGalleryFiltersState] = useState<GalleryFilters>({ rating: 0, likedOnly: false });
    const [captureRange, setCaptureRangeState] = useState<CaptureRange | null>(null);
    const [timeline, setTimeline] = useState<TimelineSummary | null>(null);
    const captureRangeRef = useRef<CaptureRange | null>(null);
    const galleryFiltersRef = useRef<GalleryFilters>({ rating: 0, likedOnly: false });
    const photoOffsetRef = useRef(0);
    const photoLoadingRef = useRef(false);
    const photoHasMoreRef = useRef(true);
    const [albums, setAlbums] = useState<Album[]>([]);
    const [albumsLoading, setAlbumsLoading] = useState<boolean>(true);
    // Populated by fetchAlbums's primary (local-index) path -- lets openAlbum
    // resolve an album's photos via the existing batched
    // /api/photos/lookup-batch instead of GET /albums/<id>'s sequential
    // per-photo point-read loop. Empty (album not found here) means the
    // fallback path (GET /albums/<id>) should be used instead.
    const [albumPhotos, setAlbumPhotos] = useState<Record<string, Photo[]>>({});
    // Keyed per album id -- a single shared flag raced when switching albums
    // quickly: a fast fetch for album B finishing after a slow fetch for album
    // A was still in flight cleared the flag for both, so A's still-loading,
    // still-undefined photo list briefly read as "no photos in this album".
    const [albumPhotosLoadingIds, setAlbumPhotosLoadingIds] = useState<Record<string, boolean>>({});
    const [placesState, setPlacesState] = useState<Place[]>([]);
    const [thingsState, setThingsState] = useState<ThingTag[]>([]);
    const [exploreLoading, setExploreLoading] = useState<boolean>(true);
    const [people, setPeople] = useState<Person[]>([]);
    const [peopleLoading, setPeopleLoading] = useState<boolean>(true);
    const [peopleTotal, setPeopleTotal] = useState(0);
    const [peopleUnnamedTotal, setPeopleUnnamedTotal] = useState(0);
    const [peopleHasMore, setPeopleHasMore] = useState(false);
    const [peopleUnavailable, setPeopleUnavailable] = useState(false);
    const [extraPeople, setExtraPeople] = useState<Record<string, Person>>({});
    const peopleOffsetRef = useRef(0);
    const peopleLoadingMoreRef = useRef(false);
    // Photo deletion updates loaded person counts immediately. Keep the
    // resulting upper bound until the People index publishes a count at or
    // below it, so an older snapshot cannot put the deleted face back.
    const peopleFaceCountCeilingsRef = useRef<Map<string, number>>(new Map());
    const peopleFaceCountsRef = useRef<Map<string, number>>(new Map());
    // Successful person deletes are immediately authoritative in the person
    // table, but the paged People index can remain stale while its replacement
    // is being published. Keep those ids hidden so a refresh cannot resurrect
    // a tile whose next delete would correctly return 404.
    const deletedPeopleRef = useRef<Set<string>>(new Set());
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
    const [albumTrash, setAlbumTrash] = useState<AlbumTrashItem[]>([]);
    const [albumTrashLoading, setAlbumTrashLoading] = useState<boolean>(false);
    const [selection, setSelection] = useState<string[]>([]);
    const [selectMode, setSelectModeState] = useState<boolean>(false);
    const [viewer, setViewer] = useState<ViewerState | null>(null);
    const [toasts, setToasts] = useState<Toast[]>([]);
    const toastTimers = useRef<Record<string, number>>({});

    // Photos fetched outside the paginated gallery list (currently: Ask's
    // /photos/search results) -- merged into photoIndex below so the viewer
    // can resolve them by id. Without this, opening a search result whose
    // photo isn't among the ~100 most-recent gallery photos found nothing,
    // and the viewer's resync effect immediately closed itself.
    const [extraPhotos, setExtraPhotos] = useState<Record<string, Photo>>({});
    const registerPhotos = useCallback((list: Photo[]) => {
        if (!list.length) return;
        setExtraPhotos((prev) => {
            let changed = false;
            const next = { ...prev };
            for (const p of list) {
                if (next[p.id] !== p) {
                    next[p.id] = p;
                    changed = true;
                }
            }
            return changed ? next : prev;
        });
    }, []);

    const photoIndex = useMemo(() => {
        const m = new Map<string, Photo>();
        for (const p of Object.values(extraPhotos)) m.set(p.id, p);
        for (const list of Object.values(albumPhotos)) for (const p of list) m.set(p.id, p);
        for (const list of Object.values(personPhotos)) for (const p of list) m.set(p.id, p);
        // The paginated gallery list is the freshest/most authoritative source
        // (rating/like edits land here first), so it's merged last and wins.
        for (const p of photos) m.set(p.id, p);
        return m;
    }, [photos, albumPhotos, personPhotos, extraPhotos]);

    const photoById = useCallback((id: string) => photoIndex.get(id), [photoIndex]);
    const photosByIds = useCallback(
        (ids: string[]) => ids.map((id) => photoIndex.get(id)).filter((p): p is Photo => Boolean(p)),
        [photoIndex],
    );
    const albumById = useCallback((id: string) => albums.find((a) => a.id === id), [albums]);
    const personById = useCallback((id: string) => (
        deletedPeopleRef.current.has(id) ? undefined : people.find((p) => p.id === id) ?? extraPeople[id]
    ), [people, extraPeople]);

    useEffect(() => {
        const counts = new Map<string, number>();
        for (const person of people) counts.set(person.id, Math.max(0, person.faceCount ?? 0));
        for (const person of Object.values(extraPeople)) counts.set(person.id, Math.max(0, person.faceCount ?? 0));
        peopleFaceCountsRef.current = counts;
    }, [people, extraPeople]);

    const applyPhotoDeleteCount = useCallback((count: number) => {
        const amount = Math.max(0, Math.floor(Number(count) || 0));
        if (!amount) return;
        setTotalPhotos((current) => {
            if (current === null) return current;
            const next = Math.max(0, current - amount);
            photoCountCeilingRef.current = photoCountCeilingRef.current === null
                ? next
                : Math.min(photoCountCeilingRef.current, next);
            return next;
        });
    }, []);

    const applyPhotoRestoreCount = useCallback((count: number) => {
        const amount = Math.max(0, Math.floor(Number(count) || 0));
        if (!amount) return;
        setTotalPhotos((current) => {
            if (current === null) return current;
            const next = current + amount;
            if (photoCountCeilingRef.current !== null) photoCountCeilingRef.current += amount;
            return next;
        });
    }, []);

    const applyServerPhotoTotal = useCallback((serverTotal: number) => {
        const total = Math.max(0, Math.floor(Number(serverTotal) || 0));
        const ceiling = photoCountCeilingRef.current;
        if (ceiling !== null && total > ceiling) {
            setTotalPhotos(ceiling);
            return;
        }
        photoCountCeilingRef.current = null;
        setTotalPhotos(total);
    }, []);

    // Server-paged fetch: the gallery's path for libraries too large for a client-side sort index
    // (see isServerPagedLibrary) and its fallback whenever the index is unavailable. The backend
    // answers from the library database with one indexed SQL page plus a few batched row reads, so
    // a page costs the same at 10k or 10M photos. With the media token, thumbnails are built
    // client-side from the returned blob names (directMedia), and a page is sized to the screen.
    const fetchPhotosViaLegacyEndpoint = useCallback(async (offset: number, reset: boolean) => {
        const range = captureRangeRef.current;
        const rangeQuery = `${range?.start ? `&captureStart=${encodeURIComponent(range.start)}` : ''}${range?.end ? `&captureEnd=${encodeURIComponent(range.end)}` : ''}`;
        const filters = galleryFiltersRef.current;
        const filterQuery = `${filters.rating > 0 ? `&rating=${filters.rating}` : ''}${filters.likedOnly ? '&minLikes=1' : ''}`;
        const token = getCachedMediaToken();
        const pageSize = token ? pageSizeForCapacity(measureGridCapacity()) : PAGE_SIZE;
        const res = await get<{ photos?: BackendPhoto[]; total?: number; indexBuilding?: boolean; indexPartial?: boolean }>(
            `/photos?sort=capture&offset=${offset}&limit=${pageSize}${rangeQuery}${filterQuery}${token ? '&directMedia=1' : ''}`,
        );
        reportIndexBuilding('gallery', Boolean(res?.indexBuilding || res?.indexPartial));
        const list = Array.isArray(res?.photos) ? res.photos.map((p) => mapPhoto(p)) : [];
        photoOffsetRef.current = offset + list.length;
        photoHasMoreRef.current = list.length === pageSize;
        setHasMorePhotos(photoHasMoreRef.current);
        if (typeof res?.total === 'number') applyServerPhotoTotal(res.total);
        setPhotos((prev) => (reset ? list : [...prev, ...list]));
    }, [applyServerPhotoTotal]);

    // Server-paged photo fetch. Primary path: download the whole-library
    // sort-index once per session (see localSortIndex.ts), sort/paginate it
    // locally, then a single targeted /api/photos/lookup-batch call for just
    // this page's full photo data -- so a scroll/page-load no longer makes
    // the backend materialize and sort the entire library per request (see
    // the sort-index's module comment in storage_utils.py). Falls back to
    // fetchPhotosViaLegacyEndpoint above on any failure (cold library with no
    // index built yet, network error, anything) so the gallery never breaks.
    const fetchPhotos = useCallback(async (reset: boolean) => {
        if (photoLoadingRef.current) return;
        if (!reset && !photoHasMoreRef.current) return;
        photoLoadingRef.current = true;
        setPhotosLoading(true);
        const offset = reset ? 0 : photoOffsetRef.current;
        try {
            // Token and sort index load in parallel (both are normally already warm
            // from the session-start preload).
            const [sortIndex, token] = await Promise.all([
                withIndexRetry(getLocalSortIndex),
                getMediaToken().catch(() => null),
            ]);
            if (!sortIndex) {
                throw new Error('sort index unavailable');
            }
            const range = captureRangeRef.current;
            const startMs = range?.start ? new Date(range.start).getTime() : null;
            const endMs = range?.end ? new Date(range.end).getTime() : null;
            const rangeFiltered = (startMs === null && endMs === null)
                ? sortIndex
                : sortIndex.filter((row) => {
                    if (!row.captureDate) return false;
                    const t = new Date(row.captureDate).getTime();
                    if (Number.isNaN(t)) return false;
                    if (startMs !== null && t < startMs) return false;
                    if (endMs !== null && t > endMs) return false;
                    return true;
                });
            const sorted = sortRowsForGallery(filterRowsForGallery(rangeFiltered, galleryFiltersRef.current));
            const total = sorted.length;
            // With the media token, thumbnails cost no backend calls, so load as
            // many tiles as the screen needs (a few viewports' worth) per step.
            const pageSize = token ? pageSizeForCapacity(measureGridCapacity()) : PAGE_SIZE;
            const pageRows = sorted.slice(offset, offset + pageSize);
            const pageFilenames = pageRows.map((row) => row.filename);
            photoOffsetRef.current = offset + pageFilenames.length;
            photoHasMoreRef.current = offset + pageFilenames.length < total;
            setHasMorePhotos(photoHasMoreRef.current);
            applyServerPhotoTotal(total);
            if (token && pageRows.length) {
                // 1) Backend-free paint of the whole window.
                const provisional = pageRows.map((row) => provisionalPhoto(row, token));
                setPhotos((prev) => (reset ? provisional : [...prev, ...provisional]));
                // 2) Enrich with full metadata in parallel chunks (first chunk =
                //    the top of the window, which is what's on screen). directMedia
                //    skips URL signing; each chunk lands as soon as it returns.
                await Promise.all(chunk(pageFilenames, ENRICH_CHUNK).map(async (names) => {
                    // A failed chunk is non-fatal: its tiles are already on screen
                    // from the sort index; they just keep the provisional metadata.
                    const res = await post<{ photos?: BackendPhoto[] }>('/api/photos/lookup-batch', { filenames: names, directMedia: true })
                        .catch(() => null);
                    if (!res) return;
                    const byFilename = new Map((res?.photos ?? []).map((p) => [p.filename, p]));
                    const enriched = new Map<string, Photo>();
                    for (const f of names) {
                        const b = byFilename.get(f);
                        if (b) enriched.set(f, mapPhoto(b, token));
                    }
                    const nameSet = new Set(names);
                    // lookup-batch may drop a filename deleted since the index build.
                    setPhotos((prev) => prev
                        .filter((p) => !nameSet.has(p.id) || enriched.has(p.id))
                        .map((p) => enriched.get(p.id) ?? p));
                }));
            } else {
                // No token (proxy mode): same as before -- wait for lookup-batch.
                const lookupRes = pageFilenames.length
                    ? await post<{ photos?: BackendPhoto[] }>('/api/photos/lookup-batch', { filenames: pageFilenames })
                    : { photos: [] };
                const byFilename = new Map((lookupRes?.photos ?? []).map((p) => [p.filename, p]));
                const list = pageFilenames
                    .map((f) => byFilename.get(f))
                    .filter((p): p is BackendPhoto => Boolean(p))
                    .map((p) => mapPhoto(p, null));
                setPhotos((prev) => (reset ? list : [...prev, ...list]));
            }
        } catch {
            try {
                await fetchPhotosViaLegacyEndpoint(offset, reset);
            } catch {
                photoHasMoreRef.current = false;
                setHasMorePhotos(false);
            }
        } finally {
            photoLoadingRef.current = false;
            setPhotosLoading(false);
        }
    }, [applyServerPhotoTotal, fetchPhotosViaLegacyEndpoint]);

    // Queued (not called directly) so this doesn't race fetchTimeline below
    // for the same backend -- both used to fire in the same mount tick.
    // Interactive callers (loadMorePhotos, reloadPhotos, setCaptureRange,
    // the viewer-prefetch effect below) deliberately call fetchPhotos
    // directly, not through the queue -- scrolling/pagination needs to stay
    // immediate, not wait behind other boot/tab work. See the 2026-10-01
    // boot-request audit.
    useEffect(() => {
        void enqueueBackgroundRequest(() => fetchPhotos(true)).catch(() => {});
    }, [fetchPhotos]);

    const loadMorePhotos = useCallback(() => { void fetchPhotos(false); }, [fetchPhotos]);
    const reloadPhotos = useCallback(() => {
        photoOffsetRef.current = 0;
        photoHasMoreRef.current = true;
        void fetchPhotos(true);
    }, [fetchPhotos]);

    // Timeline summary (year/month/day counts) drives the gallery's date rail.
    const fetchTimeline = useCallback(async () => {
        try {
            const res = await get<TimelineSummary>('/photos/timeline');
            if (!res || typeof res !== 'object' || !res.years) return;
            setTimeline(res);
            const coverNames = new Set<string>();
            Object.values(res.years).forEach((year) => {
                if (year.coverFilename) coverNames.add(year.coverFilename);
                Object.values(year.months).forEach((month) => {
                    if (month.coverFilename) coverNames.add(month.coverFilename);
                });
            });
            if (!coverNames.size) return;
            const coverUrls = new Map<string, string>();
            const batches = await Promise.all(
                chunk(Array.from(coverNames), 500).map((names) => resolveThumbnailAccessUrls(names)),
            );
            batches.forEach((batch) => batch.forEach((url, filename) => coverUrls.set(filename, url)));
            Object.values(res.years).forEach((year) => {
                if (year.coverFilename) year.coverThumbnailUrl = coverUrls.get(year.coverFilename) || undefined;
                Object.values(year.months).forEach((month) => {
                    if (month.coverFilename) month.coverThumbnailUrl = coverUrls.get(month.coverFilename) || undefined;
                });
            });
            setTimeline((current) => (current === res ? { ...res, years: { ...res.years } } : current));
        } catch {
            // timeline rail simply stays hidden on failure
        }
    }, []);

    // Queued behind fetchPhotos above -- see that effect's comment.
    useEffect(() => {
        void enqueueBackgroundRequest(() => fetchTimeline()).catch(() => {});
    }, [fetchTimeline]);

    // Narrow the gallery to a capture-date window (from the timeline rail) and
    // re-fetch from the top. Passing null clears the window back to the full set.
    const setCaptureRange = useCallback((range: CaptureRange | null) => {
        captureRangeRef.current = range;
        setCaptureRangeState(range);
        photoOffsetRef.current = 0;
        photoHasMoreRef.current = true;
        void fetchPhotos(true);
    }, [fetchPhotos]);

    const setMediaFilter = useCallback((filter: MediaFilter) => setMediaFilterState(filter), []);

    const setGalleryFilters = useCallback((filters: GalleryFilters) => {
        galleryFiltersRef.current = filters;
        setGalleryFiltersState(filters);
        photoOffsetRef.current = 0;
        photoHasMoreRef.current = true;
        void fetchPhotos(true);
    }, [fetchPhotos]);

    const setGalleryRating = useCallback((rating: number) => {
        const next = Math.max(0, Math.min(5, Math.floor(Number(rating) || 0)));
        if (galleryFiltersRef.current.rating === next) return;
        setGalleryFilters({ ...galleryFiltersRef.current, rating: next });
    }, [setGalleryFilters]);

    const setGalleryLikedOnly = useCallback((likedOnly: boolean) => {
        if (galleryFiltersRef.current.likedOnly === likedOnly) return;
        setGalleryFilters({ ...galleryFiltersRef.current, likedOnly });
    }, [setGalleryFilters]);

    const jumpToGalleryDate = useCallback(async (date: string): Promise<string | null> => {
        const clean = String(date || '').trim();
        if (!/^\d{4}-\d{2}-\d{2}$/.test(clean)) return null;
        const endMs = new Date(`${clean}T23:59:59.999Z`).getTime();
        if (!Number.isFinite(endMs)) return null;
        if (photoLoadingRef.current) return null;

        captureRangeRef.current = null;
        setCaptureRangeState(null);
        photoOffsetRef.current = 0;
        photoHasMoreRef.current = true;
        photoLoadingRef.current = true;
        setPhotosLoading(true);

        try {
            const pageSize = pageSizeForCapacity(measureGridCapacity());
            const [sortIndex, token] = await Promise.all([
                withIndexRetry(getLocalSortIndex),
                getMediaToken().catch(() => null),
            ]);
            if (!sortIndex) throw new Error('sort index unavailable');

            const sorted = sortRowsForGallery(filterRowsForGallery(sortIndex, galleryFiltersRef.current));
            if (!sorted.length) {
                setPhotos([]);
                setTotalPhotos(0);
                photoHasMoreRef.current = false;
                setHasMorePhotos(false);
                return null;
            }
            let offset = sorted.findIndex((row) => rowCaptureTime(row) <= endMs);
            if (offset < 0) offset = sorted.length - 1;
            const pageRows = sorted.slice(offset, offset + pageSize);
            const pageFilenames = pageRows.map((row) => row.filename);
            const target = pageRows[0]?.filename ?? null;
            photoOffsetRef.current = offset + pageFilenames.length;
            photoHasMoreRef.current = offset + pageFilenames.length < sorted.length;
            setHasMorePhotos(photoHasMoreRef.current);
            applyServerPhotoTotal(sorted.length);

            if (token && pageRows.length) {
                setPhotos(pageRows.map((row) => provisionalPhoto(row, token)));
                await Promise.all(chunk(pageFilenames, ENRICH_CHUNK).map(async (names) => {
                    const res = await post<{ photos?: BackendPhoto[] }>('/api/photos/lookup-batch', { filenames: names, directMedia: true })
                        .catch(() => null);
                    if (!res) return;
                    const byFilename = new Map((res?.photos ?? []).map((p) => [p.filename, p]));
                    const enriched = new Map<string, Photo>();
                    for (const f of names) {
                        const b = byFilename.get(f);
                        if (b) enriched.set(f, mapPhoto(b, token));
                    }
                    const nameSet = new Set(names);
                    setPhotos((prev) => prev
                        .filter((p) => !nameSet.has(p.id) || enriched.has(p.id))
                        .map((p) => enriched.get(p.id) ?? p));
                }));
            } else {
                const lookupRes = pageFilenames.length
                    ? await post<{ photos?: BackendPhoto[] }>('/api/photos/lookup-batch', { filenames: pageFilenames })
                    : { photos: [] };
                const byFilename = new Map((lookupRes?.photos ?? []).map((p) => [p.filename, p]));
                setPhotos(pageFilenames
                    .map((f) => byFilename.get(f))
                    .filter((p): p is BackendPhoto => Boolean(p))
                    .map((p) => mapPhoto(p, null)));
            }
            return target;
        } catch {
            try {
                const filters = galleryFiltersRef.current;
                const token = getCachedMediaToken();
                const pageSize = token ? pageSizeForCapacity(measureGridCapacity()) : PAGE_SIZE;
                const res = await get<{ offset?: number; filename?: string; total?: number; indexBuilding?: boolean; indexPartial?: boolean }>(
                    `/photos/date-position?date=${encodeURIComponent(clean)}${filters.rating > 0 ? `&rating=${filters.rating}` : ''}${filters.likedOnly ? '&minLikes=1' : ''}`,
                );
                reportIndexBuilding('gallery', Boolean(res?.indexBuilding || res?.indexPartial));
                if (typeof res?.total === 'number' && res.total <= 0) {
                    setPhotos([]);
                    setTotalPhotos(0);
                    photoHasMoreRef.current = false;
                    setHasMorePhotos(false);
                    return null;
                }
                const offset = Math.max(0, Number(res?.offset) || 0);
                await fetchPhotosViaLegacyEndpoint(offset, true);
                photoHasMoreRef.current = offset + pageSize < (res?.total ?? photoOffsetRef.current);
                setHasMorePhotos(photoHasMoreRef.current);
                return res?.filename ?? null;
            } catch {
                return null;
            }
        } finally {
            photoLoadingRef.current = false;
            setPhotosLoading(false);
        }
    }, [applyServerPhotoTotal, fetchPhotosViaLegacyEndpoint]);

    // The id sequence a gallery-backed viewer slides through: the loaded photos
    // in the current media filter. It grows as more pages load.
    const galleryViewerIds = useMemo(() => {
        const list = mediaFilter === 'all'
            ? photos
            : photos.filter((p) => (mediaFilter === 'video' ? isVideoFilename(p.filename) : !isVideoFilename(p.filename)));
        return list.map((p) => p.id);
    }, [photos, mediaFilter]);

    // Keep an open gallery-backed viewer in sync as newly-loaded photos arrive,
    // so the user can keep sliding past what was loaded when they opened it.
    useEffect(() => {
        setViewer((prev) => {
            if (!prev || !prev.extendable || prev.ids.length === galleryViewerIds.length) return prev;
            return { ...prev, ids: galleryViewerIds };
        });
    }, [galleryViewerIds]);

    // Prefetch the next page once the viewer nears the end of the loaded ids.
    useEffect(() => {
        if (!viewer?.extendable) return;
        if (viewer.index >= viewer.ids.length - 3 && photoHasMoreRef.current && !photoLoadingRef.current) {
            void fetchPhotos(false);
        }
    }, [viewer?.extendable, viewer?.index, viewer?.ids.length, fetchPhotos]);

    const dismissToast = useCallback((id: string) => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
        const timer = toastTimers.current[id];
        if (timer) {
            window.clearTimeout(timer);
            delete toastTimers.current[id];
        }
    }, []);

    const toast = useCallback(
        (message: string, actionLabel?: string, onAction?: () => void, tone: 'info' | 'error' = 'info') => {
            const id = nextId();
            setToasts((prev) => [...prev, { id, message, actionLabel, onAction, tone }]);
            toastTimers.current[id] = window.setTimeout(() => {
                setToasts((prev) => prev.filter((t) => t.id !== id));
                delete toastTimers.current[id];
                // Errors linger a little longer so a failure isn't missed.
            }, tone === 'error' ? 6500 : 4200);
        },
        [],
    );

    const navigate = useCallback((page: PageId, params: RouteParams = {}) => {
        setRoute({ page, params });
        setSelection([]);
        setSelectModeState(false);
        setViewer(null);
        window.scrollTo(0, 0);
    }, []);

    const toggleSelect = useCallback((id: string) => {
        setSelection((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
    }, []);
    const selectMany = useCallback((ids: string[]) => setSelection(ids), []);
    // Turning select mode off also drops the current selection; turning it on
    // just reveals the checkboxes.
    const setSelectMode = useCallback((on: boolean) => {
        setSelectModeState(on);
        if (!on) setSelection([]);
    }, []);
    const clearSelection = useCallback(() => { setSelection([]); setSelectModeState(false); }, []);

    const openViewer = useCallback((ids: string[], index: number, opts?: { extendable?: boolean }) => (
        setViewer({ ids, index, extendable: opts?.extendable })
    ), []);
    const closeViewer = useCallback(() => setViewer(null), []);
    const viewerStep = useCallback((delta: number) => {
        setViewer((prev) => {
            if (!prev) return prev;
            const index = Math.min(prev.ids.length - 1, Math.max(0, prev.index + delta));
            return { ...prev, index };
        });
    }, []);

    // "Show in Gallery": open one photo in the viewer, reliably, wherever it
    // lives in the library. If it's already on a loaded gallery page, open it
    // in that (swipeable, extendable) sequence; otherwise resolve it by exact
    // filename via /photos/lookup -- an exact point lookup that always finds
    // the photo, unlike scrolling/paging forward until it happens to load
    // (which never terminates for a photo thousands of items deep). The looked
    // -up photo is registered so the viewer can render it as a standalone item.
    const focusPhoto = useCallback((filename: string) => {
        const loadedIndex = photos.findIndex((p) => p.id === filename);
        if (loadedIndex >= 0) {
            openViewer(photos.map((p) => p.id), loadedIndex, { extendable: true });
            return;
        }
        openViewer([filename], 0);
        void get<{ photo?: BackendPhoto }>(`/photos/lookup/${encodeURIComponent(filename)}`)
            .then((res) => {
                if (res?.photo) registerPhotos([mapPhoto(res.photo)]);
            })
            .catch(() => { /* viewer will show its own empty/loading state */ });
    }, [photos, openViewer, registerPhotos]);

    // Primary path: the whole-library albums index (see localAlbumsIndex.ts),
    // downloaded once per session -- covers picked from the sort index, not
    // list_albums's whole-library scan. Falls back to the legacy GET /albums
    // on any failure (cold index, network error) so the page never breaks.
    const fetchAlbumsViaLegacyEndpoint = useCallback(async () => {
        const res = await get<{ albums?: Album[] }>('/albums');
        setAlbums(Array.isArray(res?.albums) ? res.albums : []);
    }, []);

    const fetchAlbums = useCallback(async () => {
        setAlbumsLoading(true);
        try {
            const rows = await withIndexRetry(getLocalAlbumsIndex);
            if (!rows) {
                throw new Error('albums index unavailable');
            }
            const coverFilenames = rows.map((r) => r.coverFilename).filter(Boolean);
            // Covers resolve from the sort index's thumbnail blob names + the one container
            // token: no backend call, and the same URL the gallery uses, so the browser
            // cache serves a thumbnail already loaded there. Only covers the sort index
            // can't name (older rows, other formats) fall back to the per-photo batch call.
            const [coverSortRows, coverToken] = await Promise.all([
                getLocalSortIndex().catch(() => null),
                getMediaToken().catch(() => null),
            ]);
            const thumbByName = new Map<string, string>();
            (coverSortRows ?? []).forEach((r) => { if (r.thumb) thumbByName.set(r.filename, r.thumb); });
            const covers = new Map<string, string>();
            const needBatch: string[] = [];
            coverFilenames.forEach((filename) => {
                const direct = coverToken ? thumbnailUrlForBlob(thumbByName.get(filename), coverToken) : '';
                if (direct) covers.set(filename, direct); else needBatch.push(filename);
            });
            if (needBatch.length) {
                (await resolveThumbnailAccessUrls(needBatch)).forEach((url, filename) => covers.set(filename, url));
            }
            const mapped: Album[] = rows.map((r) => ({
                id: r.albumId,
                name: r.name,
                photoCount: r.photoCount,
                coverThumbnailUrl: r.coverFilename ? covers.get(r.coverFilename) || undefined : undefined,
                isPublic: r.isPublic,
                publicUrl: r.publicUrl || undefined,
                publicExpiresAt: r.publicExpiresAt || undefined,
                hasAccessCode: r.hasAccessCode,
                isExpired: r.isExpired,
            }));
            setAlbums(mapped);
        } catch {
            try {
                await fetchAlbumsViaLegacyEndpoint();
            } catch {
                // leave the current list in place on transient failures
            }
        } finally {
            setAlbumsLoading(false);
        }
    }, [fetchAlbumsViaLegacyEndpoint]);

    const reloadAlbums = useCallback(() => { void fetchAlbums(); }, [fetchAlbums]);

    // Rating/liking a photo can change which photo is an album's auto-picked
    // cover (backend picks the highest-rated/most-liked photo in the album),
    // so reload albums after either succeeds to pick up any new cover.
    const ratePhotos = useCallback(
        (ids: string[], rating: number) => {
            if (!ids.length) return;
            const set = new Set(ids);
            const prevRatings = new Map(photos.filter((p) => set.has(p.id)).map((p) => [p.id, p.rating]));
            setPhotos((prev) => prev.map((p) => (set.has(p.id) ? { ...p, rating } : p)));
            // Keep the local sort-index in step so a re-sort/re-scroll this
            // session reflects the new rating immediately, without waiting on
            // the backend's independent sort-index manifest to catch up.
            ids.forEach((id) => patchLocalSortIndexRow(id, { rating }));
            void post('/photos/rate-multiple', { filenames: ids, rating })
                .then(() => { invalidateLocalAlbumsIndex(); reloadAlbums(); })
                .catch(() => {
                    setPhotos((prev) => prev.map((p) => (prevRatings.has(p.id) ? { ...p, rating: prevRatings.get(p.id)! } : p)));
                    prevRatings.forEach((prevRating, id) => patchLocalSortIndexRow(id, { rating: prevRating }));
                    toast('Couldn’t save rating', undefined, undefined, 'error');
                });
        },
        [photos, toast, reloadAlbums],
    );

    const toggleLike = useCallback(
        (id: string) => {
            const current = photos.find((p) => p.id === id);
            const nextLiked = !current?.liked;
            const prevLikes = current?.likes ?? 0;
            const optimisticLikes = Math.max(0, prevLikes + (nextLiked ? 1 : -1));
            setPhotos((prev) => prev.map((p) => (p.id === id ? { ...p, liked: nextLiked, likes: optimisticLikes } : p)));
            patchLocalSortIndexRow(id, { likes: optimisticLikes });
            void post<{ liked?: boolean; likes?: number }>(`/photos/${encodeURIComponent(id)}/like`, {})
                .then((res) => {
                    const likes = res.likes ?? optimisticLikes;
                    setPhotos((prev) => prev.map((p) => (p.id === id
                        ? { ...p, liked: res.liked ?? nextLiked, likes }
                        : p)));
                    patchLocalSortIndexRow(id, { likes });
                    invalidateLocalAlbumsIndex();
                    reloadAlbums();
                })
                .catch(() => {
                    setPhotos((prev) => prev.map((p) => (p.id === id
                        ? { ...p, liked: Boolean(current?.liked), likes: prevLikes }
                        : p)));
                    patchLocalSortIndexRow(id, { likes: prevLikes });
                    toast('Couldn’t update like', undefined, undefined, 'error');
                });
        },
        [photos, toast, reloadAlbums],
    );

    // Persist a manual rotation into every client-side photo collection so the
    // gallery grid, an already-open viewer, and any later reopen of the photo
    // all reflect it. The backend doesn't bake manual rotation into the served
    // preview/thumbnail (only EXIF orientation), so the absolute `rotation`
    // value stored here is what the viewer/grid apply as a CSS transform.
    const applyPhotoRotation = useCallback((filename: string, rotation: number) => {
        const norm = ((rotation % 360) + 360) % 360;
        const patch = (p: Photo): Photo => (p.id === filename && p.rotation !== norm ? { ...p, rotation: norm } : p);
        setPhotos((prev) => prev.map(patch));
        setAlbumPhotos((prev) => {
            const next: Record<string, Photo[]> = {};
            for (const [id, list] of Object.entries(prev)) next[id] = list.map(patch);
            return next;
        });
        setPersonPhotos((prev) => {
            const next: Record<string, Photo[]> = {};
            for (const [id, list] of Object.entries(prev)) next[id] = list.map(patch);
            return next;
        });
        setExtraPhotos((prev) => {
            const existing = prev[filename];
            if (!existing || existing.rotation === norm) return prev;
            return { ...prev, [filename]: { ...existing, rotation: norm } };
        });
    }, []);

    const adjustPeopleCountsForPhotos = useCallback((list: Photo[], delta: number) => {
        const counts = new Map<string, number>();
        for (const photo of list) {
            const personIds = Array.from(new Set(photo.personIds ?? []));
            for (const personId of personIds) counts.set(personId, (counts.get(personId) ?? 0) + delta);
        }
        if (!counts.size) return;
        counts.forEach((change, personId) => {
            const current = peopleFaceCountsRef.current.get(personId);
            if (current === undefined) return;
            const next = Math.max(0, current + change);
            peopleFaceCountsRef.current.set(personId, next);
            const ceiling = peopleFaceCountCeilingsRef.current.get(personId);
            if (change < 0) {
                peopleFaceCountCeilingsRef.current.set(personId, ceiling === undefined ? next : Math.min(ceiling, next));
            } else if (ceiling !== undefined) {
                peopleFaceCountCeilingsRef.current.set(personId, ceiling + change);
            }
        });
        const patch = (person: Person): Person => {
            const change = counts.get(person.id) ?? 0;
            if (!change) return person;
            return { ...person, faceCount: Math.max(0, (person.faceCount ?? 0) + change) };
        };
        setPeople((prev) => prev.map(patch).filter((p) => p.name || (p.faceCount ?? 0) > 0));
        setExtraPeople((prev) => {
            let changed = false;
            const next: Record<string, Person> = {};
            for (const [id, person] of Object.entries(prev)) {
                const patched = patch(person);
                if (patched !== person) changed = true;
                if (patched.name || (patched.faceCount ?? 0) > 0) next[id] = patched;
            }
            return changed ? next : prev;
        });
    }, []);

    const reconcilePersonFaceCount = useCallback((person: Person): Person => {
        const serverCount = Math.max(0, person.faceCount ?? 0);
        const ceiling = peopleFaceCountCeilingsRef.current.get(person.id);
        if (ceiling === undefined || serverCount <= ceiling) {
            if (ceiling !== undefined) peopleFaceCountCeilingsRef.current.delete(person.id);
            peopleFaceCountsRef.current.set(person.id, serverCount);
            return person;
        }
        peopleFaceCountsRef.current.set(person.id, ceiling);
        return { ...person, faceCount: ceiling };
    }, []);

    const removeFromEverywhere = useCallback((ids: string[]) => {
        const set = new Set(ids);
        setPhotos((prev) => prev.filter((p) => !set.has(p.id)));
        setExtraPhotos((prev) => {
            let changed = false;
            const next = { ...prev };
            for (const id of ids) {
                if (id in next) {
                    delete next[id];
                    changed = true;
                }
            }
            return changed ? next : prev;
        });
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
        const trashed = trash.filter((t) => set.has(t.photo.id)).map((t) => t.photo);
        const moved = trashed.length ? trashed : photosByIds(ids);
        if (moved.length) setPhotos((cur) => [...moved, ...cur]);
        setTrash((prev) => prev.filter((t) => !set.has(t.photo.id)));
        invalidateLocalSortIndex();
        invalidateLocalPeopleIndex();
        invalidateLocalAlbumsIndex();
        adjustPeopleCountsForPhotos(moved, 1);
        applyPhotoRestoreCount(moved.length);
        void post('/photos/trash/restore', { filenames: ids }).then(() => {
            publishLibraryChange('photos-restored', ['photos', 'people', 'albums', 'explore', 'trash'], { itemCount: ids.length });
        }).catch(() => {
            const movedSet = new Set(moved.map((p) => p.id));
            setPhotos((cur) => cur.filter((p) => !movedSet.has(p.id)));
            adjustPeopleCountsForPhotos(moved, -1);
            applyPhotoDeleteCount(moved.length);
            setTrash((prev) => [...moved.map((photo) => ({ photo, purgesInDays: 30 })), ...prev]);
            toast('Couldn’t restore photos', undefined, undefined, 'error');
        });
    }, [adjustPeopleCountsForPhotos, photosByIds, toast, trash, applyPhotoDeleteCount, applyPhotoRestoreCount]);

    const deletePhotos = useCallback(
        (ids: string[]) => {
            if (!ids.length) return;
            const doomed = photosByIds(ids);
            setTrash((prev) => [...doomed.map((photo) => ({ photo, purgesInDays: 30 })), ...prev]);
            removeLocalSortIndexRows(ids);
            invalidateLocalPeopleIndex();
            invalidateLocalAlbumsIndex();
            adjustPeopleCountsForPhotos(doomed, -1);
            removeFromEverywhere(ids);
            applyPhotoDeleteCount(ids.length);
            setSelection([]);
            void post<{ jobId?: string; status?: string }>('/photos/delete', { filenames: ids }).then((response) => {
                if (response?.status === 'queued') {
                    toast(`Moving ${ids.length} photos to Recently Deleted`);
                    requestJobPoll();
                } else {
                    toast(
                        `Deleted ${ids.length} photo${ids.length > 1 ? 's' : ''}`,
                        'Undo',
                        () => restorePhotos(ids),
                    );
                    publishLibraryChange('photos-deleted', ['photos', 'people', 'albums', 'explore', 'trash'], {
                        itemCount: ids.length,
                    });
                }
            }).catch(() => {
                // Roll back the optimistic removal on failure.
                const set = new Set(ids);
                setTrash((prev) => prev.filter((t) => !set.has(t.photo.id)));
                setPhotos((cur) => [...doomed, ...cur]);
                invalidateLocalSortIndex();
                adjustPeopleCountsForPhotos(doomed, 1);
                applyPhotoRestoreCount(ids.length);
                toast('Couldn’t delete photos', undefined, undefined, 'error');
            });
        },
        [photosByIds, adjustPeopleCountsForPhotos, removeFromEverywhere, toast, restorePhotos, applyPhotoDeleteCount, applyPhotoRestoreCount],
    );

    const reloadTrash = useCallback(async () => {
        setTrashLoading(true);
        try {
            const now = Date.now();
            // Page through the whole trash (200 per request) so nothing past the first page is hidden.
            const items: TrashItem[] = [];
            for (;;) {
                const res = await get<{ photos?: (BackendPhoto & { purgeAt?: string })[]; total?: number }>(`/photos/trash?limit=200&offset=${items.length}`);
                const page = Array.isArray(res?.photos) ? res.photos : [];
                items.push(...page.map((p) => {
                    const purgeAt = (p as { purgeAt?: string }).purgeAt;
                    const days = purgeAt ? Math.max(0, Math.ceil((new Date(purgeAt).getTime() - now) / 86400000)) : 30;
                    return { photo: mapPhoto(p), purgesInDays: days };
                }));
                setTrash(items.slice());
                if (page.length < 200 || (typeof res?.total === 'number' && items.length >= res.total)) break;
            }
        } catch {
            // keep the current list on failure
        } finally {
            setTrashLoading(false);
        }
    }, []);

    const restoreAllTrash = useCallback(() => {
        const snapshot = trash;
        setTrash([]);
        const moved = snapshot.map((t) => t.photo);
        setPhotos((cur) => [...moved, ...cur]);
        invalidateLocalSortIndex();
        invalidateLocalPeopleIndex();
        invalidateLocalAlbumsIndex();
        adjustPeopleCountsForPhotos(moved, 1);
        applyPhotoRestoreCount(moved.length);
        void post('/photos/trash/restore-all', {})
            .then(() => toast('Restored everything from Recently Deleted'))
            .catch(() => {
                setTrash(snapshot);
                adjustPeopleCountsForPhotos(moved, -1);
                applyPhotoDeleteCount(moved.length);
                toast('Couldn’t restore everything', undefined, undefined, 'error');
            });
    }, [adjustPeopleCountsForPhotos, trash, toast, applyPhotoDeleteCount, applyPhotoRestoreCount]);

    const purgePhoto = useCallback((id: string) => {
        const snapshot = trash;
        setTrash((prev) => prev.filter((t) => t.photo.id !== id));
        void post('/photos/trash/purge', { filenames: [id] }).catch(() => {
            setTrash(snapshot);
            toast('Couldn’t delete photo', undefined, undefined, 'error');
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
                toast('Couldn’t empty Recently Deleted', undefined, undefined, 'error');
            });
    }, [trash, toast]);

    const reloadAlbumTrash = useCallback(async () => {
        setAlbumTrashLoading(true);
        try {
            const res = await get<{ albums?: Album[] }>('/albums/trash');
            const now = Date.now();
            const items: AlbumTrashItem[] = Array.isArray(res?.albums)
                ? res.albums.map((album) => {
                    const days = album.purgeAt ? Math.max(0, Math.ceil((new Date(album.purgeAt).getTime() - now) / 86400000)) : 30;
                    return { album, purgesInDays: days };
                })
                : [];
            setAlbumTrash(items);
        } catch {
            // keep the current list on failure
        } finally {
            setAlbumTrashLoading(false);
        }
    }, []);

    const restoreAlbum = useCallback((id: string) => {
        const snapshot = albumTrash;
        const restored = snapshot.find((t) => t.album.id === id);
        setAlbumTrash((prev) => prev.filter((t) => t.album.id !== id));
        if (restored) setAlbums((prev) => [restored.album, ...prev]);
        invalidateLocalAlbumsIndex();
        void post(`/albums/${encodeURIComponent(id)}/restore`, {}).catch(() => {
            setAlbumTrash(snapshot);
            if (restored) setAlbums((prev) => prev.filter((a) => a.id !== id));
            toast('Couldn’t restore album', undefined, undefined, 'error');
        });
    }, [albumTrash, toast]);

    const purgeAlbum = useCallback((id: string) => {
        const snapshot = albumTrash;
        setAlbumTrash((prev) => prev.filter((t) => t.album.id !== id));
        invalidateLocalAlbumsIndex();
        void post(`/albums/${encodeURIComponent(id)}/purge`, {}).catch(() => {
            setAlbumTrash(snapshot);
            toast('Couldn’t permanently delete album', undefined, undefined, 'error');
        });
    }, [albumTrash, toast]);

    // No longer fetched unconditionally on app mount -- AlbumsPage's own
    // mount effect enqueues this (queue-managed, canceled on tab-leave) only
    // when the user actually visits Albums. See the 2026-10-01 boot-request
    // audit: this was one of six independent fetches StoreProvider fired in
    // parallel in the same mount tick regardless of which tab was open.

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

    // No longer fetched unconditionally on app mount -- see the fetchAlbums
    // comment above; ExplorePage's own mount effect enqueues this instead.

    const reloadExplore = useCallback(() => { void fetchExplore(); }, [fetchExplore]);

    // Album contents are served by the backend a screenful at a time and more are fetched only as the
    // user scrolls (a 50,000-photo album is never pulled in one go).
    const ALBUM_PAGE = 120;
    const albumLoadSeq = useRef<Record<string, number>>({});
    const albumOffsets = useRef<Record<string, number>>({});
    const albumLoadingMore = useRef<Record<string, boolean>>({});
    const [albumPaging, setAlbumPaging] = useState<Record<string, { total: number; hasMore: boolean }>>({});
    const fetchAlbumPage = useCallback(async (id: string, offset: number) => {
        const res = await get<{ album?: Album; photos?: BackendPhoto[]; hasMore?: boolean; total?: number }>(
            `/albums/${encodeURIComponent(id)}?offset=${offset}&limit=${ALBUM_PAGE}${getCachedMediaToken() ? '&directMedia=1' : ''}`,
        );
        const page = Array.isArray(res?.photos) ? res.photos.map((p) => mapPhoto(p)) : [];
        albumOffsets.current[id] = offset + page.length;
        setAlbumPaging((prev) => ({ ...prev, [id]: { total: res?.total ?? page.length, hasMore: Boolean(res?.hasMore) && page.length > 0 } }));
        return { page, album: res?.album };
    }, []);
    const openAlbum = useCallback(async (id: string) => {
        const seq = (albumLoadSeq.current[id] ?? 0) + 1;
        albumLoadSeq.current[id] = seq;
        const stale = () => albumLoadSeq.current[id] !== seq;
        setAlbumPhotosLoadingIds((prev) => ({ ...prev, [id]: true }));
        try {
            const { page, album } = await fetchAlbumPage(id, 0);
            if (stale()) return;
            setAlbumPhotos((prev) => ({ ...prev, [id]: page }));
            if (album) setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, ...album } : a)));
        } catch {
            setAlbumPhotos((prev) => ({ ...prev, [id]: prev[id] ?? [] }));
        } finally {
            if (!stale()) setAlbumPhotosLoadingIds((prev) => ({ ...prev, [id]: false }));
        }
    }, [fetchAlbumPage]);
    const loadMoreAlbumPhotos = useCallback((id: string) => {
        if (albumLoadingMore.current[id] || !albumPaging[id]?.hasMore) return;
        albumLoadingMore.current[id] = true;
        const seq = albumLoadSeq.current[id];
        void fetchAlbumPage(id, albumOffsets.current[id] ?? 0)
            .then(({ page }) => {
                if (albumLoadSeq.current[id] !== seq) return;
                setAlbumPhotos((prev) => {
                    const have = prev[id] ?? [];
                    const seen = new Set(have.map((p) => p.id));
                    return { ...prev, [id]: [...have, ...page.filter((p) => !seen.has(p.id))] };
                });
            })
            .catch(() => setAlbumPaging((prev) => ({ ...prev, [id]: { total: prev[id]?.total ?? 0, hasMore: false } })))
            .finally(() => { albumLoadingMore.current[id] = false; });
    }, [fetchAlbumPage, albumPaging]);
    const albumPhotosTotal = useCallback((id: string) => albumPaging[id]?.total, [albumPaging]);
    const albumPhotosHasMore = useCallback((id: string) => Boolean(albumPaging[id]?.hasMore), [albumPaging]);

    const albumPhotosById = useCallback((id: string) => albumPhotos[id], [albumPhotos]);
    const isAlbumPhotosLoading = useCallback((id: string) => Boolean(albumPhotosLoadingIds[id]), [albumPhotosLoadingIds]);

    const createAlbum = useCallback(
        async (name?: string): Promise<string> => {
            const finalName = (name ?? 'New album').trim() || 'New album';
            try {
                const res = await post<{ album?: Album }>('/albums', { name: finalName });
                const created = res?.album;
                if (created) {
                    setAlbums((prev) => [...prev, created]);
                    invalidateLocalAlbumsIndex();
                    publishLibraryChange('album-created', ['albums'], { itemCount: 1 });
                    return created.id;
                }
            } catch {
                toast('Couldn’t create album', undefined, undefined, 'error');
            }
            return '';
        },
        [toast],
    );

    // Smart albums: server picks the matching photos by `rule` (location,
    // recent-upload, person, event-window, tag-object -- see SMART_ALBUM_RULES
    // in AlbumsPage.tsx) and creates/returns the album in one call.
    const autoCreateAlbum = useCallback(
        async (rule: string): Promise<{ albumId: string; count: number; message?: string }> => {
            try {
                const res = await post<{ album?: Album; count?: number; message?: string }>('/albums/autocreate', { rule });
                const created = res?.album;
                const count = Number(res?.count || 0);
                if (count > 0 && created) {
                    setAlbums((prev) => (prev.some((a) => a.id === created.id) ? prev : [created, ...prev]));
                    invalidateLocalAlbumsIndex();
                    publishLibraryChange('album-created', ['albums'], { itemCount: 1 });
                    return { albumId: created.id, count };
                }
                return { albumId: '', count: 0, message: res?.message };
            } catch {
                toast('Couldn’t create smart album', undefined, undefined, 'error');
                return { albumId: '', count: 0 };
            }
        },
        [toast],
    );

    const renameAlbum = useCallback((id: string, name: string) => {
        const trimmed = name.trim();
        if (!trimmed) return;
        const previous = albums.find((a) => a.id === id)?.name;
        setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, name: trimmed } : a)));
        invalidateLocalAlbumsIndex();
        void post(`/albums/${encodeURIComponent(id)}/rename`, { name: trimmed }).then(() => {
            publishLibraryChange('album-renamed', ['albums'], { itemCount: 1 });
        }).catch(() => {
            setAlbums((prev) => prev.map((a) => (a.id === id ? { ...a, name: previous ?? a.name } : a)));
            toast('Couldn’t rename album', undefined, undefined, 'error');
        });
    }, [albums, toast]);

    const addPhotosToAlbum = useCallback(
        (albumId: string, ids: string[]) => {
            if (!ids.length) return;
            const album = albums.find((a) => a.id === albumId);
            const idSet = new Set(ids);
            const added = photosByIds(ids);
            setAlbums((prev) => prev.map((a) => (a.id === albumId ? { ...a, photoCount: a.photoCount + ids.length } : a)));
            // Optimistically drop the added photos into the album's photo cache
            // so opening the album shows them *immediately* -- otherwise a brand
            // new album (or one whose fetch is slow) appears empty for as long
            // as the server round-trip takes, which read as "nothing happened"
            // for minutes. The server fetch below reconciles order/cover.
            if (added.length) {
                setAlbumPhotos((prev) => {
                    const existing = prev[albumId] ?? [];
                    const rest = existing.filter((p) => !idSet.has(p.id));
                    return { ...prev, [albumId]: [...added, ...rest] };
                });
            }
            invalidateLocalAlbumsIndex();
            void post(`/albums/${encodeURIComponent(albumId)}/photos/add`, { filenames: ids })
                .then(() => {
                    publishLibraryChange('album-photos-added', ['albums'], { itemCount: ids.length });
                    toast(`Added ${ids.length} to “${album?.name ?? 'album'}”`);
                    // Reconcile with the server (cover, ordering). The cache is
                    // already seeded above, so this refresh never shows a spinner.
                    void openAlbum(albumId);
                })
                .catch(() => {
                    setAlbums((prev) => prev.map((a) => (a.id === albumId ? { ...a, photoCount: Math.max(0, a.photoCount - ids.length) } : a)));
                    setAlbumPhotos((prev) => (prev[albumId]
                        ? { ...prev, [albumId]: prev[albumId].filter((p) => !idSet.has(p.id)) }
                        : prev));
                    toast('Couldn’t add photos to album', undefined, undefined, 'error');
                });
        },
        [albums, photosByIds, openAlbum, toast],
    );

    const deleteAlbum = useCallback((id: string) => {
        const removed = albums.find((a) => a.id === id);
        setAlbums((prev) => prev.filter((a) => a.id !== id));
        invalidateLocalAlbumsIndex();
        void post('/albums/delete-multiple', { albumIds: [id] })
            .then(() => {
                publishLibraryChange('album-deleted', ['albums'], { itemCount: 1 });
                toast(`Deleted “${removed?.name ?? 'album'}”`);
            })
            .catch(() => {
                if (removed) setAlbums((prev) => [...prev, removed]);
                toast('Couldn’t delete album', undefined, undefined, 'error');
            });
    }, [albums, toast]);

    const deleteAlbums = useCallback((ids: string[]) => {
        if (!ids.length) return;
        const idSet = new Set(ids);
        const removed = albums.filter((a) => idSet.has(a.id));
        setAlbums((prev) => prev.filter((a) => !idSet.has(a.id)));
        invalidateLocalAlbumsIndex();
        void post('/albums/delete-multiple', { albumIds: ids })
            .then(() => {
                publishLibraryChange('albums-deleted', ['albums'], { itemCount: ids.length });
                toast(`Deleted ${removed.length} album${removed.length === 1 ? '' : 's'}`);
            })
            .catch(() => {
                if (removed.length) setAlbums((prev) => [...prev, ...removed]);
                toast('Couldn’t delete albums', undefined, undefined, 'error');
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
            invalidateLocalAlbumsIndex();
        } catch {
            toast('Couldn’t update share link', undefined, undefined, 'error');
        }
    }, [toast]);

    const revokeAlbum = useCallback(async (id: string) => {
        try {
            const res = await post<{ album?: Album }>(`/albums/${encodeURIComponent(id)}/revoke`, {});
            setAlbums((prev) => prev.map((a) => (a.id === id
                ? { ...a, ...(res?.album ?? {}), isPublic: false, publicUrl: undefined }
                : a)));
            invalidateLocalAlbumsIndex();
        } catch {
            toast('Couldn’t revoke share link', undefined, undefined, 'error');
        }
    }, [toast]);

    // Primary path: one server page of clusters (named first), cut from the cached people index, then
    // more as the user scrolls -- the browser never downloads every cluster. If the index is not
    // available yet, show the preparing state and retry this paged route instead of falling back to
    // the legacy full-list endpoint.
    const reconcileDeletedPeople = useCallback(async (): Promise<{
        complete: boolean;
        indexed: Map<string, PeoplePageRow>;
    }> => {
        const ids = Array.from(deletedPeopleRef.current);
        const indexed = new Map<string, PeoplePageRow>();
        if (!ids.length) return { complete: true, indexed };
        try {
            // Keep URLs comfortably below proxy limits when a large selection
            // was deleted. The endpoint returns exact ids from the same index
            // snapshot used by the People page.
            for (let start = 0; start < ids.length; start += 50) {
                const chunk = ids.slice(start, start + 50);
                const encoded = encodeURIComponent(chunk.join(','));
                const res = await get<PeoplePageResponse>(`/api/persons/page?ids=${encoded}&limit=${chunk.length}`);
                if (!res?.available || !Array.isArray(res.rows)) return { complete: false, indexed };
                for (const row of res.rows) indexed.set(row.personId, row);
            }
        } catch {
            return { complete: false, indexed };
        }
        // Keep successful tombstones for this StoreProvider's lifetime. Person
        // UUIDs are never reused, and page/id reads can briefly hit different
        // replicas while a new index is being published.
        return { complete: true, indexed };
    }, []);

    const fetchPeople = useCallback(async () => {
        setPeopleLoading(true);
        try {
            const res = await get<PeoplePageResponse>(`/api/persons/page?offset=0&limit=${PEOPLE_PAGE}`);
            if (!res?.available || !Array.isArray(res.rows)) {
                // The server answered "index not ready": show the preparing bar and let it retry.
                setPeopleHasMore(false);
                setPeopleUnavailable(true);
                reportIndexBuilding('people', true);
                return;
            }
            const reconciliation = await reconcileDeletedPeople();
            const mapped = res.rows
                .filter((row) => !deletedPeopleRef.current.has(row.personId))
                .map(mapPersonRow)
                .map(reconcilePersonFaceCount)
                .filter((person) => person.name || (person.faceCount ?? 0) > 0);
            // Offset tracks rows consumed from the server, including hidden
            // tombstones, otherwise load-more would request overlapping rows.
            peopleOffsetRef.current = res.rows.length;
            setPeople(mapped);
            if (reconciliation.complete) {
                const indexedDeleted = Array.from(reconciliation.indexed.values());
                const deletedUnnamed = indexedDeleted.filter((row) => !row.isNamed).length;
                setPeopleTotal(Math.max(0, (res.total ?? mapped.length) - indexedDeleted.length));
                setPeopleUnnamedTotal(Math.max(0, (res.unnamedCount ?? 0) - deletedUnnamed));
            }
            setPeopleHasMore(Boolean(res.hasMore));
            setPeopleUnavailable(false);
            reportIndexBuilding('people', false);
        } catch {
            // Never fall back to fetching every cluster at once: show a "preparing" state and let the
            // user (or the next visit) retry the paged request.
            // A failed request (404/5xx/network) is not "building": don't claim the library is being prepared.
            console.warn('people page request failed');
            setPeopleHasMore(false);
            setPeopleUnavailable(true);
            reportIndexBuilding('people', false);
        } finally {
            setPeopleLoading(false);
        }
    }, [reconcileDeletedPeople, reconcilePersonFaceCount]);

    const loadMorePeople = useCallback(() => {
        if (peopleLoadingMoreRef.current) return;
        peopleLoadingMoreRef.current = true;
        void get<PeoplePageResponse>(`/api/persons/page?offset=${peopleOffsetRef.current}&limit=${PEOPLE_PAGE}`)
            .then((res) => {
                const rawRows = Array.isArray(res?.rows) ? res.rows : [];
                const rows = rawRows
                    .filter((row) => !deletedPeopleRef.current.has(row.personId))
                    .map(mapPersonRow)
                    .map(reconcilePersonFaceCount)
                    .filter((person) => person.name || (person.faceCount ?? 0) > 0);
                peopleOffsetRef.current += rawRows.length;
                setPeople((prev) => {
                    const seen = new Set(prev.map((p) => p.id));
                    return [...prev, ...rows.filter((p) => !seen.has(p.id))];
                });
                setPeopleHasMore(Boolean(res?.hasMore) && rawRows.length > 0);
            })
            .catch(() => setPeopleHasMore(false))
            .finally(() => { peopleLoadingMoreRef.current = false; });
    }, [reconcilePersonFaceCount]);

    const searchPeople = useCallback(async (query: string, limit = 50): Promise<Person[]> => {
        try {
            const res = await get<PeoplePageResponse>(`/api/persons/page?q=${encodeURIComponent(query)}&limit=${limit}`);
            return Array.isArray(res?.rows)
                ? res.rows
                    .filter((row) => !deletedPeopleRef.current.has(row.personId))
                    .map(mapPersonRow)
                    .map(reconcilePersonFaceCount)
                    .filter((person) => person.name || (person.faceCount ?? 0) > 0)
                : [];
        } catch {
            return [];
        }
    }, [reconcilePersonFaceCount]);

    // A deep link to a person who isn't in the loaded pages: fetch just that cluster.
    const ensurePerson = useCallback(async (id: string) => {
        if (deletedPeopleRef.current.has(id)) return;
        try {
            const res = await get<PeoplePageResponse>(`/api/persons/page?ids=${encodeURIComponent(id)}&limit=1`);
            const row = Array.isArray(res?.rows) ? res.rows[0] : undefined;
            if (row && !deletedPeopleRef.current.has(row.personId)) {
                const person = reconcilePersonFaceCount(mapPersonRow(row));
                setExtraPeople((prev) => ({ ...prev, [row.personId]: person }));
            }
        } catch {
            // the page shows "no longer exists"
        }
    }, [reconcilePersonFaceCount]);

    // No longer fetched unconditionally on app mount -- see the fetchAlbums
    // comment above; PeoplePage's own mount effect enqueues this instead.

    const reloadPeople = useCallback(() => { void fetchPeople(); }, [fetchPeople]);

    // A person can have tens of thousands of photos: the server returns their faces best-first a page
    // at a time, and the page appends the next one as the user scrolls (no cap on how many can load).
    const PERSON_PAGE = 120;
    const personFaceOffsets = useRef<Record<string, number>>({});
    const [personPaging, setPersonPaging] = useState<Record<string, { total: number; hasMore: boolean }>>({});
    const personLoadingMore = useRef<Record<string, boolean>>({});
    // A person's photos, best face first: extras returns one page of filenames from the person-membership
    // table (a single small partition read, exact total), and one lookup-batch call on the backend turns them
    // into photo records with token-built thumbnails. If the membership endpoint fails, fall back to the
    // person's face list so the page is never empty when the person has faces.
    const fetchPersonPage = useCallback(async (id: string, offset: number) => {
        try {
            const page = await get<{ filenames?: string[]; total?: number; hasMore?: boolean }>(
                `/api/persons/${encodeURIComponent(id)}/photos?offset=${offset}&limit=${PERSON_PAGE}`,
            );
            const names = Array.isArray(page?.filenames) ? page.filenames : [];
            let list: Photo[] = [];
            if (names.length) {
                const res = await post<{ photos?: BackendPhoto[] }>('/api/photos/lookup-batch', { filenames: names, directMedia: Boolean(getCachedMediaToken()) });
                const byName = new Map((res?.photos ?? []).map((p) => [p.filename, p]));
                list = names.map((n) => byName.get(n)).filter((p): p is BackendPhoto => Boolean(p)).map((p) => mapPhoto(p));
            }
            personFaceOffsets.current[id] = offset + names.length;
            setPersonPaging((prev) => ({ ...prev, [id]: { total: page?.total ?? names.length, hasMore: Boolean(page?.hasMore) && names.length > 0 } }));
            return list;
        } catch {
            // fall through to the face list
        }
        const res = await get<{ faces?: PersonFace[]; total?: number; hasMore?: boolean }>(
            `/api/persons/${encodeURIComponent(id)}?offset=${offset}&limit=${PERSON_PAGE}`,
        );
        const faces = Array.isArray(res?.faces) ? res.faces : [];
        personFaceOffsets.current[id] = offset + faces.length;
        setPersonPaging((prev) => ({ ...prev, [id]: { total: res?.total ?? faces.length, hasMore: Boolean(res?.hasMore) && faces.length > 0 } }));
        return facesToPhotos(faces);
    }, []);

    const openPerson = useCallback(async (id: string) => {
        void ensurePerson(id);
        setPersonPhotosLoading(true);
        try {
            personFaceOffsets.current[id] = 0;
            const first = await fetchPersonPage(id, 0);
            setPersonPhotos((prev) => ({ ...prev, [id]: first }));
        } catch {
            setPersonPhotos((prev) => ({ ...prev, [id]: prev[id] ?? [] }));
        } finally {
            setPersonPhotosLoading(false);
        }
    }, [fetchPersonPage, ensurePerson]);

    const loadMorePersonPhotos = useCallback((id: string) => {
        if (personLoadingMore.current[id] || !personPaging[id]?.hasMore) return;
        personLoadingMore.current[id] = true;
        void fetchPersonPage(id, personFaceOffsets.current[id] ?? 0)
            .then((more) => setPersonPhotos((prev) => {
                const have = prev[id] ?? [];
                const seen = new Set(have.map((p) => p.id));
                return { ...prev, [id]: [...have, ...more.filter((p) => !seen.has(p.id))] };
            }))
            .catch(() => setPersonPaging((prev) => ({ ...prev, [id]: { total: prev[id]?.total ?? 0, hasMore: false } })))
            .finally(() => { personLoadingMore.current[id] = false; });
    }, [fetchPersonPage, personPaging]);

    const personPhotosTotal = useCallback((id: string) => personPaging[id]?.total, [personPaging]);
    const personPhotosHasMore = useCallback((id: string) => Boolean(personPaging[id]?.hasMore), [personPaging]);

    const personPhotosById = useCallback((id: string) => personPhotos[id], [personPhotos]);

    const renamePerson = useCallback((id: string, name: string) => {
        const trimmed = name.trim();
        const previous = people.find((p) => p.id === id)?.name ?? null;
        setPeople((prev) => prev.map((p) => (p.id === id ? { ...p, name: trimmed || null } : p)));
        setExtraPeople((prev) => (prev[id] ? { ...prev, [id]: { ...prev[id], name: trimmed || null } } : prev));
        invalidateLocalPeopleIndex();
        void faceService.labelPerson(id, trimmed).then(() => {
            publishLibraryChange('person-renamed', ['people'], { itemCount: 1 });
        }).catch(() => {
            setPeople((prev) => prev.map((p) => (p.id === id ? { ...p, name: previous } : p)));
            setExtraPeople((prev) => (prev[id] ? { ...prev, [id]: { ...prev[id], name: previous } } : prev));
            invalidateLocalPeopleIndex();
            toast('Couldn’t save name', undefined, undefined, 'error');
        });
    }, [people, toast]);

    const mergePeople = useCallback(
        (sourceId: string, targetId: string) => {
            // Optimistically drop the source cluster; the target absorbs it.
            const removed = people.find((p) => p.id === sourceId) ?? extraPeople[sourceId];
            const movedPhotos = personPhotos[sourceId] ?? [];
            deletedPeopleRef.current.add(sourceId);
            setPeople((prev) => prev.filter((p) => p.id !== sourceId));
            setExtraPeople((prev) => {
                const next = { ...prev };
                delete next[sourceId];
                if (next[targetId] && removed) next[targetId] = { ...next[targetId], faceCount: (next[targetId].faceCount ?? 0) + (removed.faceCount ?? 0) };
                return next;
            });
            setPeople((prev) => prev.map((p) => (p.id === targetId && removed ? { ...p, faceCount: (p.faceCount ?? 0) + (removed.faceCount ?? 0) } : p)));
            if (movedPhotos.length) {
                setPersonPhotos((prev) => {
                    const target = prev[targetId] ?? [];
                    const seen = new Set(target.map((p) => p.id));
                    const merged = [...target, ...movedPhotos.filter((p) => !seen.has(p.id))];
                    const next = { ...prev, [targetId]: merged };
                    delete next[sourceId];
                    return next;
                });
            } else {
                setPersonPhotos((prev) => {
                    if (!(sourceId in prev)) return prev;
                    const next = { ...prev };
                    delete next[sourceId];
                    return next;
                });
            }
            invalidateLocalPeopleIndex();
            void faceService.mergePersons(targetId, [sourceId])
                .then(() => {
                    publishLibraryChange('people-merged', ['people'], { itemCount: 2, entityIds: [sourceId] });
                    toast('People merged');
                    void fetchPeople();
                })
                .catch(() => {
                    deletedPeopleRef.current.delete(sourceId);
                    if (removed) setPeople((prev) => [...prev, removed]);
                    invalidateLocalPeopleIndex();
                    toast('Couldn’t merge people', undefined, undefined, 'error');
                });
        },
        [people, extraPeople, personPhotos, fetchPeople, toast],
    );

    // Multi-select merge from the People grid: several source clusters into
    // one target in a single request (the backend's merge endpoint already
    // accepts multiple mergeIds -- no separate "batch" call needed for a
    // single target).
    const mergePeopleBatch = useCallback(
        (targetId: string, sourceIds: string[]) => {
            const ids = sourceIds.filter((id) => id !== targetId);
            if (!ids.length) return;
            const removedSet = new Set(ids);
            const removed = people.filter((p) => removedSet.has(p.id));
            const movedCount = removed.reduce((sum, p) => sum + (p.faceCount ?? 0), 0);
            ids.forEach((id) => deletedPeopleRef.current.add(id));
            setPeople((prev) => prev.filter((p) => !removedSet.has(p.id)));
            setExtraPeople((prev) => {
                const next = { ...prev };
                for (const id of ids) delete next[id];
                if (next[targetId]) next[targetId] = { ...next[targetId], faceCount: (next[targetId].faceCount ?? 0) + movedCount };
                return next;
            });
            if (movedCount) setPeople((prev) => prev.map((p) => (p.id === targetId ? { ...p, faceCount: (p.faceCount ?? 0) + movedCount } : p)));
            setPersonPhotos((prev) => {
                let changed = false;
                const next = { ...prev };
                const target = next[targetId] ?? [];
                const seen = new Set(target.map((p) => p.id));
                const merged = [...target];
                for (const id of ids) {
                    const sourcePhotos = next[id] ?? [];
                    for (const photo of sourcePhotos) {
                        if (!seen.has(photo.id)) {
                            seen.add(photo.id);
                            merged.push(photo);
                        }
                    }
                    if (id in next) {
                        delete next[id];
                        changed = true;
                    }
                }
                if (changed && merged.length) next[targetId] = merged;
                return changed ? next : prev;
            });
            invalidateLocalPeopleIndex();
            void faceService.mergePersons(targetId, ids)
                .then(() => {
                    publishLibraryChange('people-merged', ['people'], { itemCount: ids.length + 1, entityIds: ids });
                    toast(`Merged ${ids.length + 1} people`);
                    void fetchPeople();
                })
                .catch(() => {
                    ids.forEach((id) => deletedPeopleRef.current.delete(id));
                    if (removed.length) setPeople((prev) => [...prev, ...removed]);
                    invalidateLocalPeopleIndex();
                    toast('Couldn’t merge people', undefined, undefined, 'error');
                });
        },
        [people, fetchPeople, toast],
    );

    // Deletes a person cluster entirely (their faces become unassigned, not
    // deleted) -- distinct from mergePeople/mergePeopleBatch, which fold one
    // cluster's faces into another rather than dropping them.
    const deletePerson = useCallback((id: string) => {
        const removed = people.find((p) => p.id === id) ?? extraPeople[id];
        deletedPeopleRef.current.add(id);
        setPeople((prev) => prev.filter((p) => p.id !== id));
        if (removed) {
            setPeopleTotal((prev) => Math.max(0, prev - 1));
            if (!removed.name) setPeopleUnnamedTotal((prev) => Math.max(0, prev - 1));
        }
        setExtraPeople((prev) => {
            if (!prev[id]) return prev;
            const next = { ...prev };
            delete next[id];
            return next;
        });
        setPersonPhotos((prev) => {
            if (!prev[id]) return prev;
            const next = { ...prev };
            delete next[id];
            return next;
        });
        invalidateLocalPeopleIndex();
        void faceService.deletePersons([id])
            .then(() => {
                publishLibraryChange('person-deleted', ['people'], { itemCount: 1, entityIds: [id] });
                toast('Person deleted');
            })
            .catch(() => {
                deletedPeopleRef.current.delete(id);
                if (removed) setPeople((prev) => [...prev, removed]);
                if (removed) {
                    setPeopleTotal((prev) => prev + 1);
                    if (!removed.name) setPeopleUnnamedTotal((prev) => prev + 1);
                }
                invalidateLocalPeopleIndex();
                toast('Couldn’t delete person', undefined, undefined, 'error');
            });
    }, [people, extraPeople, toast]);

    const deletePeopleBatch = useCallback((ids: string[]) => {
        if (!ids.length) return;
        const idSet = new Set(ids);
        const removed = ids
            .map((id) => people.find((p) => p.id === id) ?? extraPeople[id])
            .filter((person): person is Person => Boolean(person));
        idSet.forEach((id) => deletedPeopleRef.current.add(id));
        setPeople((prev) => prev.filter((p) => !idSet.has(p.id)));
        setPeopleTotal((prev) => Math.max(0, prev - removed.length));
        const removedUnnamed = removed.filter((person) => !person.name).length;
        if (removedUnnamed) setPeopleUnnamedTotal((prev) => Math.max(0, prev - removedUnnamed));
        setExtraPeople((prev) => {
            let changed = false;
            const next = { ...prev };
            for (const id of ids) {
                if (id in next) {
                    delete next[id];
                    changed = true;
                }
            }
            return changed ? next : prev;
        });
        setPersonPhotos((prev) => {
            let changed = false;
            const next = { ...prev };
            for (const id of ids) {
                if (id in next) {
                    delete next[id];
                    changed = true;
                }
            }
            return changed ? next : prev;
        });
        invalidateLocalPeopleIndex();
        void faceService.deletePersons(ids)
            .then(() => {
                publishLibraryChange('people-deleted', ['people'], { itemCount: ids.length, entityIds: ids });
                toast(`Deleted ${removed.length} ${removed.length === 1 ? 'person' : 'people'}`);
            })
            .catch(() => {
                idSet.forEach((id) => deletedPeopleRef.current.delete(id));
                if (removed.length) setPeople((prev) => [...prev, ...removed]);
                setPeopleTotal((prev) => prev + removed.length);
                if (removedUnnamed) setPeopleUnnamedTotal((prev) => prev + removedUnnamed);
                invalidateLocalPeopleIndex();
                toast('Couldn’t delete people', undefined, undefined, 'error');
            });
    }, [people, extraPeople, toast]);

    const applyExternalPeopleRemoval = useCallback((ids: string[]) => {
        if (!ids.length) return;
        const removed = new Set(ids);
        ids.forEach((id) => deletedPeopleRef.current.add(id));
        setPeople((prev) => prev.filter((person) => !removed.has(person.id)));
        setExtraPeople((prev) => {
            let changed = false;
            const next = { ...prev };
            for (const id of ids) {
                if (id in next) {
                    delete next[id];
                    changed = true;
                }
            }
            return changed ? next : prev;
        });
        setPersonPhotos((prev) => {
            let changed = false;
            const next = { ...prev };
            for (const id of ids) {
                if (id in next) {
                    delete next[id];
                    changed = true;
                }
            }
            return changed ? next : prev;
        });
    }, []);

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

    // No longer fetched unconditionally on app mount -- see the fetchAlbums
    // comment above; SharingPage's own mount effect enqueues this instead
    // (it used to ALSO call reloadMembers() itself on its own mount, so
    // visiting Sharing actually fetched members twice -- once here, once
    // there).

    const reloadMembers = useCallback(() => { void fetchMembers(); }, [fetchMembers]);

    const invite = useCallback((email: string, targetType: 'join' | 'fresh') => {
        void library.sendInvite(email, targetType)
            .then(() => { toast(`Invite sent to ${email}`); void fetchMembers(); })
            .catch((err) => toast(err instanceof Error ? err.message : 'Couldn’t send invite', undefined, undefined, 'error'));
    }, [fetchMembers, toast]);

    const revokeInvite = useCallback((inviteId: string) => {
        setPendingInvites((prev) => prev.filter((p) => p.inviteId !== inviteId));
        void library.revokePendingInvite(inviteId)
            .then(() => toast('Invitation revoked'))
            .catch(() => { toast('Couldn’t revoke invite', undefined, undefined, 'error'); void fetchMembers(); });
    }, [fetchMembers, toast]);

    const removeMember = useCallback((userId: string) => {
        setMembers((prev) => prev.filter((m) => m.userId !== userId));
        void library.removeMember(userId)
            .then(() => toast('Member removed'))
            .catch(() => { toast('Couldn’t remove member', undefined, undefined, 'error'); void fetchMembers(); });
    }, [fetchMembers, toast]);

    const renameLibrary = useCallback((name: string) => {
        const trimmed = name.trim();
        if (!trimmed) return;
        const previous = libraryName;
        setLibraryName(trimmed);
        void library.renameLibrary(trimmed)
            .then(() => toast('Library renamed'))
            .catch(() => { setLibraryName(previous); toast('Couldn’t rename library', undefined, undefined, 'error'); });
    }, [libraryName, toast]);

    // While the people list is unavailable (server still building it), retry quietly every 10 s.
    useEffect(() => {
        if (!peopleUnavailable) return undefined;
        const timer = window.setTimeout(() => { void fetchPeople(); }, 10000);
        return () => window.clearTimeout(timer);
    }, [peopleUnavailable, fetchPeople, people.length]);

    // The server finished preparing the library: pull in what was missing while it built.
    useEffect(() => onIndexReady(() => {
        reloadPhotos();
        void fetchTimeline();
        void fetchPeople();
        void fetchAlbums();
        void fetchExplore();
    }), [reloadPhotos, fetchTimeline, fetchPeople, fetchAlbums, fetchExplore]);

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
            albumTrash,
            albumTrashLoading,
            selection,
            viewer,
            toasts,
            photosLoading,
            hasMorePhotos,
            totalPhotos,
            loadMorePhotos,
            reloadPhotos,
            applyExternalPhotoDeleteCount: applyPhotoDeleteCount,
            mediaFilter,
            setMediaFilter,
            galleryFilters,
            setGalleryRating,
            setGalleryLikedOnly,
            jumpToGalleryDate,
            captureRange,
            setCaptureRange,
            timeline,
            exploreLoading,
            reloadExplore,
            fetchExplore,
            photoById,
            photosByIds,
            albumById,
            personById,
            registerPhotos,
            navigate,
            toggleSelect,
            selectMany,
            clearSelection,
            selectMode,
            setSelectMode,
            openViewer,
            closeViewer,
            viewerStep,
            focusPhoto,
            ratePhotos,
            toggleLike,
            applyPhotoRotation,
            deletePhotos,
            restorePhotos,
            restoreAllTrash,
            purgePhoto,
            purgeAllTrash,
            reloadTrash,
            reloadAlbumTrash,
            restoreAlbum,
            purgeAlbum,
            albumsLoading,
            reloadAlbums,
            fetchAlbums,
            openAlbum,
            albumPhotosById,
            isAlbumPhotosLoading,
            loadMoreAlbumPhotos,
            albumPhotosTotal,
            albumPhotosHasMore,
            createAlbum,
            autoCreateAlbum,
            renameAlbum,
            addPhotosToAlbum,
            deleteAlbum,
            deleteAlbums,
            shareAlbum,
            revokeAlbum,
            peopleLoading, peopleTotal, peopleUnnamedTotal, peopleHasMore, peopleUnavailable, loadMorePeople, searchPeople,
            reloadPeople,
            fetchPeople,
            openPerson,
            personPhotosById,
            personPhotosTotal,
            personPhotosHasMore,
            loadMorePersonPhotos,
            personPhotosLoading,
            renamePerson,
            mergePeople,
            mergePeopleBatch,
            deletePerson,
            deletePeopleBatch,
            applyExternalPeopleRemoval,
            reloadMembers,
            fetchMembers,
            invite,
            revokeInvite,
            removeMember,
            renameLibrary,
            toast,
            dismissToast,
        }),
        [
            route, photos, albums, people, members, pendingInvites, libraryName, isOwner, maxMembers, membersLoading,
            placesState, thingsState, trash, trashLoading, albumTrash, albumTrashLoading, selection, selectMode, viewer, toasts,
            photosLoading, hasMorePhotos, totalPhotos, loadMorePhotos, reloadPhotos, applyPhotoDeleteCount,
            mediaFilter, setMediaFilter, galleryFilters, setGalleryRating, setGalleryLikedOnly, jumpToGalleryDate, captureRange, setCaptureRange, timeline,
            exploreLoading, reloadExplore, fetchExplore,
            photoById, photosByIds, albumById, personById, registerPhotos, navigate, toggleSelect, selectMany,
            clearSelection, setSelectMode, openViewer, closeViewer, viewerStep, focusPhoto, ratePhotos, toggleLike, applyPhotoRotation, deletePhotos,
            restorePhotos, restoreAllTrash, purgePhoto, purgeAllTrash, reloadTrash,
            reloadAlbumTrash, restoreAlbum, purgeAlbum,
            albumsLoading, reloadAlbums, fetchAlbums, openAlbum, albumPhotosById, isAlbumPhotosLoading, loadMoreAlbumPhotos, albumPhotosTotal, albumPhotosHasMore,
            createAlbum, autoCreateAlbum, renameAlbum, addPhotosToAlbum, deleteAlbum, deleteAlbums, shareAlbum, revokeAlbum,
            peopleLoading, peopleTotal, peopleUnnamedTotal, peopleHasMore, peopleUnavailable, loadMorePeople, searchPeople, reloadPeople, fetchPeople, openPerson, personPhotosById, personPhotosTotal, personPhotosHasMore, loadMorePersonPhotos, personPhotosLoading,
            renamePerson, mergePeople, mergePeopleBatch, deletePerson, deletePeopleBatch, applyExternalPeopleRemoval, reloadMembers, fetchMembers, invite, revokeInvite,
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
