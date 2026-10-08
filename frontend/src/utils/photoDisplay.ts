export const SUPPORTED_RAW_EXTENSIONS = [
    '3fr', 'ari', 'arw', 'bay', 'braw', 'cap', 'cr2', 'cr3', 'crw', 'dcr',
    'dcs', 'dng', 'drf', 'eip', 'erf', 'fff', 'gpr', 'iiq', 'k25', 'kdc',
    'mdc', 'mef', 'mos', 'mrw', 'nef', 'nrw', 'orf', 'pef', 'ptx', 'pxn',
    'r3d', 'raf', 'raw', 'rw2', 'rwl', 'rwz', 'sr2', 'srf', 'srw', 'x3f',
] as const;

const SUPPORTED_VIDEO_EXTENSIONS = [
    '3g2', '3gp', 'avi', 'm2ts', 'm4v', 'mkv', 'mov', 'mp4',
    'mpeg', 'mpg', 'mts', 'webm', 'wmv',
] as const;

const SUPPORTED_HEIC_EXTENSIONS = ['heic', 'heif'] as const;

// JPEG XL: Pillow decodes it server-side (backend/image_utils.py), but no
// mainstream browser renders it natively via <img src>, so like HEIC/RAW it
// always needs a backend-converted JPEG preview.
const SUPPORTED_JXL_EXTENSIONS = ['jxl'] as const;

const RAW_EXTENSIONS = new Set<string>(SUPPORTED_RAW_EXTENSIONS);
const VIDEO_EXTENSIONS = new Set<string>(SUPPORTED_VIDEO_EXTENSIONS);
const HEIC_EXTENSIONS = new Set<string>(SUPPORTED_HEIC_EXTENSIONS);
const JXL_EXTENSIONS = new Set<string>(SUPPORTED_JXL_EXTENSIONS);

const BACKEND_PREVIEW_EXTENSIONS = new Set<string>([
    ...SUPPORTED_HEIC_EXTENSIONS,
    ...SUPPORTED_RAW_EXTENSIONS,
    ...SUPPORTED_JXL_EXTENSIONS,
]);

export const FILE_ACCEPT_FILTER = [
    'image/*',
    'video/*',
    '.heic',
    '.heif',
    ...SUPPORTED_RAW_EXTENSIONS.map((ext) => `.${ext}`),
    ...SUPPORTED_VIDEO_EXTENSIONS.map((ext) => `.${ext}`),
    ...SUPPORTED_JXL_EXTENSIONS.map((ext) => `.${ext}`),
].join(',');

export type MediaKind = 'RAW' | 'JPEG' | 'VIDEO';

export const getFileExtension = (filename: string): string => {
    const match = /\.([^.]+)$/.exec(filename || '');
    return match ? match[1].toLowerCase() : '';
};

export const isRawFilename = (filename: string): boolean => RAW_EXTENSIONS.has(getFileExtension(filename));

export const isVideoFilename = (filename: string): boolean => VIDEO_EXTENSIONS.has(getFileExtension(filename));

export const isHeicFilename = (filename: string): boolean => HEIC_EXTENSIONS.has(getFileExtension(filename));

export const isJxlFilename = (filename: string): boolean => JXL_EXTENSIONS.has(getFileExtension(filename));

export const requiresBackendPreview = (filename: string): boolean => (
    BACKEND_PREVIEW_EXTENSIONS.has(getFileExtension(filename))
);

// A forced re-run of preview/thumbnail exists to replace a bad stored
// preview/thumbnail blob (e.g. Tools > Force re-run after a RAW-preview bug
// fix). Fetching "the current converted preview" mid-force-rerun fetches
// exactly the bad blob being replaced -- in 'both' processing mode, that
// fetch+resubmit finishes before ipworker's real (slower) regeneration, so it
// re-locks the step to 'done' with the identical bad bytes and silently
// discards the real fix. Skip that fetch whenever this pass is force-running
// preview or thumbnail, so the real backend result is free to win.
export const shouldSkipConvertedRawPreview = (
    force: boolean,
    effectiveSteps: Set<string> | null,
): boolean => (
    force && (!effectiveSteps || effectiveSteps.has('preview') || effectiveSteps.has('thumbnail'))
);

export const getMediaKind = (filename: string): MediaKind => {
    if (isVideoFilename(filename)) {
        return 'VIDEO';
    }
    return isRawFilename(filename) ? 'RAW' : 'JPEG';
};
