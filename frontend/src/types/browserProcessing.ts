export type BrowserFaceDetection = {
    bbox: {
        left: number;
        top: number;
        width: number;
        height: number;
    };
    confidence: number;
    imageWidth: number;
    imageHeight: number;
    embedding?: number[];
    detector?: string;
};

export interface UploadProgress {
    uploadedCount: number;
    totalCount: number;
    failedCount: number;
    skippedDuplicateCount: number;
    mbPerSecond?: number;
}

export interface AppNotification {
    id: string;
    title: string;
    details: string;
    timestamp: number;
    unread: boolean;
    progress?: UploadProgress;
}

export interface FilterOptions {
    minRating: number;
    minLikes: number;
}

type PersistedUploadFileStatus = 'pending' | 'uploading' | 'finalizing' | 'done' | 'failed';

export interface PersistedUploadFile {
    key: string;
    name: string;
    size: number;
    type: string;
    lastModified: number;
    uploadedBytes: number;
    skippedDuplicate: boolean;
    status: PersistedUploadFileStatus;
    uploadId?: string;
    blockIds?: string[];
    chunkSizeBytes?: number;
    error?: string;
}

export interface PersistedUploadSession {
    id: string;
    createdAt: number;
    files: PersistedUploadFile[];
}

export interface UploadProfile {
    fileParallelism: number;
    chunkSizeBytes: number;
    reason: string;
}

export type ClientProcessingStep = 'thumbnail' | 'exif' | 'ocr' | 'ai_vision' | 'map_detection' | 'face';
export type ClientProcessingStatus = 'done' | 'skipped' | 'failed' | 'timeout' | 'unsupported';
export type ClientProcessingSourceKind = 'original' | 'raw_embedded_jpeg' | 'raw_converted_jpeg' | 'backend_converted_jpeg' | 'raw_exif_only' | 'unsupported';
type BrowserAiModelAvailability = 'available' | 'cached' | 'downloaded' | 'unavailable' | 'skipped';
export type BrowserAiModelCacheStatus = 'hit' | 'miss' | 'downloaded' | 'failed';
export type BrowserAiModelUiStatus = 'checking' | 'idle' | 'loading' | 'available' | 'unavailable' | 'unsupported';
export type BrowserFaceFailureStage =
    | 'model_load_failed'
    | 'embedding_model_load_failed'
    | 'face_api_load_failed'
    | 'detection_failed'
    | 'descriptor_failed'
    | 'timeout'
    | 'background_throttled'
    | 'unsupported_runtime'
    | 'source_unavailable'
    | 'unknown';

export type ClientProcessingReason =
    | 'done'
    | 'offline'
    | 'poor_network'
    | 'save_data_enabled'
    | 'unsupported_runtime'
    | 'model_download_timeout'
    | 'model_load_failed'
    | 'model_unavailable'
    | 'model_budget_exceeded'
    | 'file_too_large'
    | 'image_too_large'
    | 'memory_budget'
    | 'inference_timeout'
    | 'finalize_grace_expired'
    | 'background_throttled'
    | 'upstream_incomplete'
    | 'sas_expired_or_upload_retry'
    | 'user_cancelled'
    | 'raw_preview_missing'
    | 'raw_preview_invalid'
    | 'raw_container_unsupported'
    | 'raw_exif_only'
    | 'video_unsupported'
    | 'unknown_error';

export type BrowserAiNetworkGate = {
    allowed: boolean;
    reason: ClientProcessingReason | 'network_info_unavailable' | null;
    detail: string;
    hasNetworkInfo: boolean;
};

export interface ClientProcessingReportItem {
    clientAssetId: string;
    step: ClientProcessingStep;
    status: ClientProcessingStatus;
    reason: ClientProcessingReason;
    durationMs: number;
    model?: string;
    modelVersion?: string;
    modelTaxonomyVersion?: string;
    runtime?: string;
    sourceKind?: ClientProcessingSourceKind;
    sourceFormat?: string;
    rawParserVersion?: string;
    previewWidth?: number;
    previewHeight?: number;
    originalBytes?: number;
    sourceBytes?: number;
    modelAvailability?: BrowserAiModelAvailability;
    modelCacheStatus?: BrowserAiModelCacheStatus;
    modelManifestVersion?: string;
    modelAcquisitionMs?: number;
    detail?: string;
    faceModelReady?: boolean;
    embeddingsReady?: boolean;
    rawFaceCount?: number;
    detectedFaceCount?: number;
    candidateFaceCount?: number;
    filteredFaceCount?: number;
    filteredReason?: string;
    faceFailureStage?: BrowserFaceFailureStage;
    faceFailureDetail?: string;
}

export interface ClientProcessingResult {
    clientProcessing: Record<string, any>;
    clientProcessingReport: ClientProcessingReportItem[];
    lateResultPending?: boolean;
}

export type BrowserFaceDetectionResult = {
    faces: BrowserFaceDetection[];
    source?: 'native_tfjs';
    model?: string;
    modelVersion?: string;
    modelTaxonomyVersion?: string;
    runtime?: string;
    schemaVersion?: number;
    rawFaceCount?: number;
    detectedFaceCount?: number;
    candidateFaceCount?: number;
    filteredFaceCount?: number;
    filteredReason?: string;
    debugStages?: Array<
        | 'model_load_started'
        | 'model_load_done'
        | 'detection_started'
        | 'detection_done'
        | 'embedding_model_load_started'
        | 'embedding_model_load_done'
        | 'crop_started'
        | 'crop_done'
        | 'descriptor_started'
        | 'descriptor_done'
        | 'dedupe_done'
        | 'timeout'
        | 'background_throttled'
    >;
    faceFailureStage?: BrowserFaceFailureStage;
    faceFailureDetail?: string;
};

export interface BrowserAiModelState {
    status: BrowserAiModelUiStatus;
    reason?: ClientProcessingReason;
    detail?: string;
    modelAvailability: BrowserAiModelAvailability;
    modelCacheStatus?: BrowserAiModelCacheStatus;
    modelManifestVersion?: string;
    modelAcquisitionMs?: number;
    model?: string;
    modelVersion?: string;
    modelTaxonomyVersion?: string;
    runtime?: string;
    manifest?: unknown;
}

type BrowserAiManifestAsset = {
    url?: string;
    path?: string;
    bytes?: number;
    size?: number;
    sha256?: string;
};

export interface BrowserAiManifest {
    manifestVersion?: string;
    version?: string;
    runtime?: string;
    task?: string;
    model?: string;
    faceModel?: string;
    faceTask?: string;
    faceEmbeddingTask?: string;
    faceEmbeddingModel?: string;
    faceEmbeddingModelVersion?: string;
    faceEmbeddingModelTaxonomyVersion?: string;
    faceEmbeddingDescriptorDimensions?: number;
    faceEmbeddingModelUrl?: string;
    modelVersion?: string;
    modelTaxonomyVersion?: string;
    allowLocalModels?: boolean;
    allowRemoteModels?: boolean;
    enableLocalVisionFallback?: boolean;
    localModelPath?: string;
    wasmPath?: string;
    tagVocabularyUrl?: string;
    vocabTopK?: number;
    topK?: number;
    minStoredLabels?: number;
    maxCandidateLabels?: number;
    scoreThreshold?: number;
    personScoreThreshold?: number;
    faceScoreThreshold?: number;
    assets?: BrowserAiManifestAsset[];
    models?: Array<{
        name?: string;
        version?: string;
        taxonomyVersion?: string;
        assets?: BrowserAiManifestAsset[];
    }>;
}

export type BrowserAiImagePayload = {
    data: Uint8Array | Uint8ClampedArray;
    width: number;
    height: number;
    channels: 1 | 2 | 3 | 4;
};

