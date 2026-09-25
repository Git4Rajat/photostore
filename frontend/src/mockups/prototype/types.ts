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
    | 'trash';

export interface Photo {
    id: string;
    filename: string;
    swatch: SwatchKey;
    dateLabel: string;
    year: number;
    rating: number; // 0–5
    liked: boolean;
    placeId: string | null;
    personIds: string[];
    tags: string[];
}

export interface ShareSettings {
    isPublic: boolean;
    expiry: string;
    code: string;
}

export interface Album {
    id: string;
    name: string;
    coverPhotoId: string | null;
    photoIds: string[];
    share: ShareSettings;
}

export interface Person {
    id: string;
    name: string | null; // null => unnamed cluster
    swatch: SwatchKey;
    photoIds: string[];
}

export interface Place {
    id: string;
    name: string;
    swatch: SwatchKey;
}

export interface ThingTag {
    id: string;
    name: string;
    count: number;
    swatch: SwatchKey;
}

export interface Member {
    id: string;
    name: string;
    sub: string;
    initials: string;
    color: string;
    role: 'owner' | 'view' | 'contribute';
    pending?: boolean;
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

export interface RouteParams {
    albumId?: string;
    personId?: string;
    query?: string;
    placeId?: string;
    tag?: string;
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
