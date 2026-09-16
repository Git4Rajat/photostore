import { get } from './apiClient';
import { getActiveLibraryFromToken } from './passwordAuthClient';
import { parseVectorIndexNpz } from './localVectorIndexParser';

export interface PeopleNameIndex {
    pidToName: Record<string, string>;
    nameToIds: Record<string, string[]>;
}

export interface LocalVectorIndex {
    embeddingVersion: string;
    dimension: number;
    /** Keyed by filename (matches lexical index rows' RowKey). */
    embeddingsByFilename: Map<string, Float32Array>;
}

export interface LocalSearchIndex {
    rows: Record<string, unknown>[];
    peopleNameIndex: PeopleNameIndex;
    sourceVersion: string;
    updatedAt: string;
    /** null when no real (non-dirty) vector index exists yet server-side --
     * semantic scoring is skipped and search falls back to lexical-only. */
    vectorIndex: LocalVectorIndex | null;
}

interface SearchIndexResponse {
    available: boolean;
    indexUrl?: string;
    sourceVersion?: string;
    updatedAt?: string;
    peopleNameIndex?: PeopleNameIndex;
    vectorIndexUrl?: string;
    embeddingVersion?: string;
}

// Keyed by active library so switching libraries can't serve one library's
// index rows while browsing another -- same reasoning as photoCache.ts's
// photoCacheKey(). Fetched lazily on first search rather than eagerly at
// login: a session that never searches never pays for the download at all.
let cachedKey: string | null = null;
let cachedIndex: LocalSearchIndex | null = null;
let inFlight: Promise<LocalSearchIndex | null> | null = null;

const indexKey = (): string => getActiveLibraryFromToken() || '__default__';

const decompressGzip = async (buffer: ArrayBuffer): Promise<string> => {
    // DecompressionStream is the standard, dependency-free way to gunzip in a
    // browser (no polyfill/library needed) -- available in every browser this
    // app already requires for its other Streams-API-based media handling.
    const stream = new Response(buffer).body!.pipeThrough(new DecompressionStream('gzip'));
    const decompressed = await new Response(stream).text();
    return decompressed;
};

const downloadIndexBlob = async (indexUrl: string): Promise<Record<string, unknown>[]> => {
    const response = await fetch(indexUrl);
    if (!response.ok) {
        throw new Error(`Failed to download search index (${response.status})`);
    }
    const buffer = await response.arrayBuffer();
    // The blob is stored with Content-Encoding: gzip -- most browsers
    // transparently decompress it before we ever see the bytes, so try
    // parsing directly first and only fall back to manual decompression if
    // the fetch (e.g. because the CDN/proxy path stripped the header, or a
    // browser didn't auto-decode this particular response) handed back the
    // raw compressed bytes instead.
    try {
        const text = new TextDecoder().decode(buffer);
        const parsed = JSON.parse(text);
        if (parsed && Array.isArray(parsed.rows)) {
            return parsed.rows;
        }
    } catch {
        // fall through to manual gunzip
    }
    const text = await decompressGzip(buffer);
    const parsed = JSON.parse(text);
    return Array.isArray(parsed?.rows) ? parsed.rows : [];
};

const downloadVectorIndex = async (vectorIndexUrl: string, embeddingVersion: string): Promise<LocalVectorIndex | null> => {
    try {
        const response = await fetch(vectorIndexUrl);
        if (!response.ok) {
            return null;
        }
        const buffer = await response.arrayBuffer();
        const parsed = await parseVectorIndexNpz(buffer);
        const embeddingsByFilename = new Map<string, Float32Array>();
        parsed.rowKeys.forEach((filename, i) => embeddingsByFilename.set(filename, parsed.embeddings[i]));
        return { embeddingVersion: embeddingVersion || parsed.embeddingVersion, dimension: parsed.dimension, embeddingsByFilename };
    } catch {
        // Semantic search is a bonus tier over lexical -- any failure here
        // (network, a malformed blob, a parser edge case) should degrade to
        // lexical-only search, never break search entirely.
        return null;
    }
};

const fetchLocalSearchIndex = async (): Promise<LocalSearchIndex | null> => {
    const response: SearchIndexResponse = await get('/api/photos/search-index');
    if (!response?.available || !response.indexUrl) {
        return null;
    }
    const rows = await downloadIndexBlob(response.indexUrl);
    const vectorIndex = response.vectorIndexUrl
        ? await downloadVectorIndex(response.vectorIndexUrl, response.embeddingVersion || '')
        : null;
    return {
        rows,
        peopleNameIndex: response.peopleNameIndex || { pidToName: {}, nameToIds: {} },
        sourceVersion: response.sourceVersion || '',
        updatedAt: response.updatedAt || '',
        vectorIndex,
    };
};

export const getLocalSearchIndex = async (): Promise<LocalSearchIndex | null> => {
    const key = indexKey();
    if (cachedIndex && cachedKey === key) {
        return cachedIndex;
    }
    if (inFlight && cachedKey === key) {
        return inFlight;
    }
    cachedKey = key;
    inFlight = fetchLocalSearchIndex()
        .then((result) => {
            cachedIndex = result;
            return result;
        })
        .catch(() => {
            cachedIndex = null;
            return null;
        })
        .finally(() => {
            inFlight = null;
        });
    return inFlight;
};

// Called after an upload batch completes (see PhotoGallery.tsx's upload
// completion handler) so the next search picks up newly-added photos
// instead of serving a stale in-memory copy for the rest of the tab session.
export const invalidateLocalSearchIndex = (): void => {
    cachedIndex = null;
    cachedKey = null;
};
