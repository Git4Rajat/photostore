interface PhotoPersonLink {
    personId: string;
    name: string;
}

export interface Photo {
    filename: string;
    url: string;
    thumbnailUrl?: string;
    /** Physical thumbnail blob name (token mode); build the URL with mediaToken.ts. */
    thumbnailBlob?: string;
    /** Sharper grid-tile image blob name (token mode), present only once it exists. */
    gridBlob?: string;
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
    /** A Live Photo: filename of its attached motion clip. */
    liveClip?: string;
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
    /** Per-step server processing status, plus which worker (if any) currently
     * holds the processing lease -- drives the "processing on server" tile icon. */
    processing?: {
        preview?: string | null;
        thumbnail?: string | null;
        exif?: string | null;
        ocr?: string | null;
        face?: string | null;
        faceSource?: string | null;
        aiVision?: string | null;
        mapDetection?: string | null;
        activeWorker?: 'ipworker' | null;
        /** Human-diagnosable reason per non-'done' step (e.g. thumbnail's
         * 'raw_preview_extraction_failed'), keyed the same as the status
         * fields above. Only present for steps that have one. */
        reasons?: Record<string, string> | null;
    };
}
