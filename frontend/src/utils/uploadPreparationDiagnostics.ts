export const PREPARATION_ASSETS = ['preview', 'thumbnail', 'metadata'] as const;
export type PreparationAsset = typeof PREPARATION_ASSETS[number];
export interface AssetPreparationDiagnostic {
    target: boolean;
    generation: 'not_attempted' | 'generated' | 'failed' | 'skipped';
    generationReason?: string;
    generationMs?: number;
    bytes?: number;
    upload: 'not_attempted' | 'uploaded' | 'failed' | 'aborted';
    uploadReason?: string;
    uploadMs?: number;
    queueMs?: number;
    httpStatus?: number;
    attempts?: number;
    runtime?: 'embedded_video_jpeg' | 'native_video' | 'ffmpeg_wasm' | 'mediainfo_wasm';
    readBytes?: number;
    readCalls?: number;
}
export interface UploadPreparationReport {
    version: 1;
    elapsedMs: number;
    admissionMs?: number;
    reason?: string;
    assets: Record<PreparationAsset, AssetPreparationDiagnostic>;
}

// Never forward raw exception messages, URLs, EXIF values or file contents.
export function preparationFailureReason(error: unknown): string {
    const message = String(error instanceof Error ? error.message : error).toLowerCase();
    if (message.includes('video metadata unsupported')) return 'video_metadata_unsupported';
    if (message.includes('video decode unsupported') || message.includes('video mount unsupported')) return 'video_decode_unsupported';
    if (message.includes('video metadata reference')) return 'metadata_reference';
    if (message.includes('resource guard') || message.includes('byte limit') || message.includes('sensor exceeds')) return 'resource_limit';
    if (message.includes('unsupported file format') || message.includes('not raw file')) return 'unsupported_raw';
    if (message.includes('budget') || message.includes('packet limit') || message.includes('manifest limit')) return 'metadata_budget';
    if (message.includes('unsupported container') || message.includes('unsupported tiff')) return 'metadata_unsupported';
    if (message.includes('out-of-bounds') || message.includes('short read')) return 'metadata_reference';
    if (message.includes('traversal') || message.includes('ifd') || message.includes('ciff') || message.includes('jpeg')
        || message.includes('bmff') || message.includes('ctmd') || message.includes('x3f') || message.includes('png')) return 'metadata_structure';
    return 'generation_failed';
}
