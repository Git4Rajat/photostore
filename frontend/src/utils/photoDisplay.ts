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

const RAW_EXTENSIONS = new Set<string>(SUPPORTED_RAW_EXTENSIONS);
const VIDEO_EXTENSIONS = new Set<string>(SUPPORTED_VIDEO_EXTENSIONS);

const BACKEND_PREVIEW_EXTENSIONS = new Set<string>([
    'heic',
    'heif',
    ...SUPPORTED_RAW_EXTENSIONS,
]);

export const FILE_ACCEPT_FILTER = [
    'image/*',
    'video/*',
    '.heic',
    '.heif',
    ...SUPPORTED_RAW_EXTENSIONS.map((ext) => `.${ext}`),
    ...SUPPORTED_VIDEO_EXTENSIONS.map((ext) => `.${ext}`),
].join(',');

export type MediaKind = 'RAW' | 'JPEG' | 'VIDEO';

export const getFileExtension = (filename: string): string => {
    const match = /\.([^.]+)$/.exec(filename || '');
    return match ? match[1].toLowerCase() : '';
};

export const isRawFilename = (filename: string): boolean => RAW_EXTENSIONS.has(getFileExtension(filename));

export const isVideoFilename = (filename: string): boolean => VIDEO_EXTENSIONS.has(getFileExtension(filename));

export const requiresBackendPreview = (filename: string): boolean => (
    BACKEND_PREVIEW_EXTENSIONS.has(getFileExtension(filename))
);

export const getMediaKind = (filename: string): MediaKind => {
    if (isVideoFilename(filename)) {
        return 'VIDEO';
    }
    return isRawFilename(filename) ? 'RAW' : 'JPEG';
};
