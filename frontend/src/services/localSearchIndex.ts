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
// photoCacheKey(). getLocalSearchIndex() itself stays lazy/on-demand (any
// caller just awaits whatever's cached or in flight) -- PrototypeApp.tsx now
// calls it fire-and-forget right after sign-in so the download/decompress
// happens during the app shell's load instead of blocking the user's first
// Ask query, but callers that don't proactively warm it (e.g. the legacy
// PhotoGallery.tsx tree) still only pay for the download the first time they
// actually call this.
let cachedKey: string | null = null;
let cachedIndex: LocalSearchIndex | null = null;
let inFlight: Promise<LocalSearchIndex | null> | null = null;

const indexKey = (): string => getActiveLibraryFromToken() || '__default__';

// Large index blobs normally land in 20-35s; a plain fetch() has no default
// timeout, so a stalled connection (dropped mid-transfer, a starved
// HTTP/1.1 connection slot against Blob Storage's per-origin cap -- see
// azure-blob-upload-speed-ceiling) hangs this promise forever instead of
// rejecting. getLocalSearchIndex()'s module-scoped `inFlight` dedup then
// keeps returning that same permanently-pending promise to every caller
// (PrototypeApp.tsx's eager retry loop AND Ask's own on-demand call), so
// one stalled download silently wedges search for the rest of the tab
// session -- confirmed live 2026-09-29: the metadata call succeeded but the
// actual blob fetch never completed or even appeared as a finished request
// in a 4.5-minute HAR capture. Aborting after a generous ceiling lets the
// existing catch-and-clear-cache handling (see getLocalSearchIndex) treat a
// stall as a normal failure, so the next call retries with a fresh request.
const BLOB_FETCH_TIMEOUT_MS = 120000;

const fetchWithTimeout = (url: string, timeoutMs: number): Promise<Response> => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { signal: controller.signal }).finally(() => clearTimeout(timer));
};

class IndexTooLargeError extends Error {
    constructor(byteLength: number) {
        super(`Search index is too large to load in-browser (${byteLength} bytes)`);
        this.name = 'IndexTooLargeError';
    }
}

// V8 caps JS string length at ~536,870,888 UTF-16 code units -- decoding a
// gzip-decompressed index anywhere near that throws RangeError: Invalid
// string length. Fail fast below that ceiling with a typed error instead of
// letting TextDecoder/JSON.parse crash: confirmed live 2026-09-30 on
// microsvcpoc-dev, a 709MB index made every download attempt throw, and the
// warm-up retry loop below had no way to tell "permanently too big" from
// "transient failure" -- it re-downloaded the 47.5MB gzipped blob every 15s
// for up to 20 attempts, saturating the connection pool and starving
// thumbnail/image requests for the rest of the session.
const MAX_INDEX_BYTES = 480 * 1024 * 1024;

const decompressGzip = async (buffer: ArrayBuffer): Promise<string> => {
    // DecompressionStream is the standard, dependency-free way to gunzip in a
    // browser (no polyfill/library needed) -- available in every browser this
    // app already requires for its other Streams-API-based media handling.
    const stream = new Response(buffer).body!.pipeThrough(new DecompressionStream('gzip'));
    const decompressed = await new Response(stream).text();
    return decompressed;
};

const downloadIndexBlob = async (indexUrl: string): Promise<Record<string, unknown>[]> => {
    const response = await fetchWithTimeout(indexUrl, BLOB_FETCH_TIMEOUT_MS);
    if (!response.ok) {
        throw new Error(`Failed to download search index (${response.status})`);
    }
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > MAX_INDEX_BYTES) {
        throw new IndexTooLargeError(buffer.byteLength);
    }
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
        const response = await fetchWithTimeout(vectorIndexUrl, BLOB_FETCH_TIMEOUT_MS);
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

// Set when a download throws IndexTooLargeError, so repeat warm-up attempts
// for that exact sourceVersion skip straight past the multi-MB blob fetch
// instead of re-downloading it every 15s (see MAX_INDEX_BYTES above). Cleared
// automatically once the server produces a new sourceVersion (a rebuild).
let unavailableVersionKey: string | null = null;

const fetchLocalSearchIndex = async (key: string): Promise<LocalSearchIndex | null> => {
    const response: SearchIndexResponse = await get('/api/photos/search-index');
    if (!response?.available || !response.indexUrl) {
        return null;
    }
    const versionKey = `${key}::${response.sourceVersion || ''}`;
    if (versionKey === unavailableVersionKey) {
        return null;
    }
    let rows: Record<string, unknown>[];
    try {
        rows = await downloadIndexBlob(response.indexUrl);
    } catch (err) {
        if (err instanceof IndexTooLargeError) {
            unavailableVersionKey = versionKey;
        }
        throw err;
    }
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
    inFlight = fetchLocalSearchIndex(key)
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
