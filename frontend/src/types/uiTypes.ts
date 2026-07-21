export interface Photo {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    thumbnailRotation?: number;
    size: number;
    lastModified?: string | null;
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
}

export interface PhotoMetadata {
    exifData?: Record<string, string>;
    tags?: string[];
}
