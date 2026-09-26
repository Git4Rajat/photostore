// Shared types for the fully-functional Keepsake prototype (standalone, no
// backend). Kept dependency-free so both the seed data and the store can
// import from here without a cycle.

export type SwatchKey = 's1' | 's2' | 's3' | 's4' | 's5' | 's6' | 's7' | 's8';

export type PageId =
    | 'ask'
    | 'gallery'
    | 'albums'
    | 'people'
    | 'person'
    | 'explore'
    | 'sharing'
    | 'tools'
    | 'trash'
    | 'additional';

export interface Photo {
    id: string; // equals `filename` for real photos — the backend's stable key
    filename: string;
    swatch: SwatchKey; // deterministic placeholder shown behind/while the image loads
    dateLabel: string;
    year: number;
    rating: number; // 0–5
    liked: boolean;
    placeId: string | null;
    personIds: string[];
    tags: string[];
    // Real-image fields (absent in legacy seed data; present once wired to the
    // backend). `thumbnailUrl` is the raw value from the listing response — a
    // direct SAS http URL for most photos, or a backend-relative path for
    // formats that need a scoped access token (HEIC/CR3/video). PhotoGrid /
    // PhotoViewer resolve these to a loadable src via services/imageClient +
    // components/shared/PhotoTile helpers.
    thumbnailUrl?: string;
    rotation?: number;
    thumbnailRotation?: number;
    likes?: number;
    captureDate?: string | null;
    /** Per-step server processing status (mirrors the backend's raw status
     * strings -- 'done'/'pending'/'queued'/'running'/'failed'/'skipped'/
     * 'no_data'/'timeout'/'unsupported' -- or a {status} object for face).
     * Absent for legacy seed data. Drives the Workbench tile's step icons. */
    processing?: {
        preview?: string | null;
        thumbnail?: string | null;
        exif?: string | null;
        ocr?: string | null;
        face?: string | { status?: string } | null;
        aiVision?: string | null;
        mapDetection?: string | null;
    };
}

export interface Album {
    id: string;
    name: string;
    photoCount: number;
    isPublic?: boolean;
    publicUrl?: string;
    publicExpiresAt?: string;
    hasAccessCode?: boolean;
    isExpired?: boolean;
    deletedAt?: string;
    purgeAt?: string;
}

export interface Person {
    id: string;
    name: string | null; // null => unnamed cluster
    swatch: SwatchKey; // placeholder tint when no cover thumbnail is available
    photoIds: string[]; // legacy seed field; empty for server-backed people
    coverThumbnailUrl?: string; // representative face thumbnail (SAS or backend path)
    faceCount?: number;
}

export interface Place {
    id: string;
    name: string;
    swatch: SwatchKey;
    count?: number;
    coverThumbnailUrl?: string;
    latitude?: string;
    longitude?: string;
}

export interface ThingTag {
    id: string;
    name: string;
    count: number;
    swatch: SwatchKey;
    coverThumbnailUrl?: string;
}

export interface ActivityItem {
    id: string;
    label: string;
    when: string;
    actor: 'you' | 'auto';
    undoable: boolean;
}

export interface Suggestion {
    id: string;
    text: string;
    action: string;
    target?: { page: PageId; params?: RouteParams };
}

export interface TrashItem {
    photo: Photo;
    purgesInDays: number;
}

export interface AlbumTrashItem {
    album: Album;
    purgesInDays: number;
}

export interface RouteParams {
    albumId?: string;
    personId?: string;
    query?: string;
    placeId?: string;
    tag?: string;
    // Comma-separated filenames deep-linked into the Tools "Workbench" view.
    filenames?: string;
}

export interface Route {
    page: PageId;
    params: RouteParams;
}

export interface Toast {
    id: string;
    message: string;
    actionLabel?: string;
    onAction?: () => void;
}
