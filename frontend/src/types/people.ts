interface FaceBoundingBox {
    left: number;
    top: number;
    width: number;
    height: number;
}

export interface PersonFace {
    faceId?: string;
    filename?: string;
    bbox?: Partial<FaceBoundingBox>;
    imageWidth?: number;
    imageHeight?: number;
    reviewStatus?: string;
    suspiciousReason?: string;
    confidence?: number;
}

export interface PersonSummary {
    personId: string;
    name?: string;
    faceIds?: string[];
    faceCount?: number;
    representativeFace?: PersonFace;
    representativeFaces?: PersonFace[];
}

export interface MergeHistoryItem {
    mergeId?: string;
    targetPersonId?: string;
    targetName?: string;
    mergedIds?: string[];
    mergedNames?: string[];
    createdAt?: string;
}

export interface SuggestionItem {
    sourcePersonId?: string;
    targetPersonId?: string;
    similarity?: number;
    sourceFaceCount?: number;
    targetFaceCount?: number;
    sourceName?: string;
    targetName?: string;
    sourceFace?: PersonFace;
    targetFace?: PersonFace;
}

export interface PersonDetailModel extends PersonSummary {
    faces?: PersonFace[];
}
