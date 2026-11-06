interface FaceBoundingBox {
    left: number;
    top: number;
    width: number;
    height: number;
}

export interface PersonFace {
    faceId?: string;
    filename?: string;
    // Direct-blob SAS URL for the photo's thumbnail when it can be served straight
    // from storage; absent/empty when the tile must fall back to the backend proxy.
    thumbnailUrl?: string;
    bbox?: Partial<FaceBoundingBox>;
    imageWidth?: number;
    imageHeight?: number;
    reviewStatus?: string;
    suspiciousReason?: string;
    confidence?: number;
}

