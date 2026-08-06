export interface PhotoPersonLink {
    personId: string;
    name: string;
}

export interface Photo {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    thumbnailRotation?: number;
    size: number;
    lastModified?: string | null;
    /** When the photo was uploaded (ISO-8601). Drives the "Recent" sort. */
    uploadDate?: string | null;
    /** EXIF capture time, or the upload time when no capture date exists (ISO-8601). Drives the "Captured" sort. */
    captureDate?: string | null;
    rating?: number;
    likes?: number;
    liked?: boolean;
    tags?: string[];
    rotation?: number;
    location?: { latitude: string; longitude: string; address: string };
    hasExif?: boolean;
    exifSummary?: {
        camera?: string;
        lens?: string;
        capturedAt?: string;
        fNumber?: string;
        exposureTime?: string;
        iso?: string;
        focalLength?: string;
    };
    /** People identified in this photo, for the "view in cluster" quick action. */
    people?: PhotoPersonLink[];
}

export interface PhotoMetadata {
    exifData?: Record<string, string>;
    tags?: string[];
}
