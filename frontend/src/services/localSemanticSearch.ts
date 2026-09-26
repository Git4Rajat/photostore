/**
 * Full client-side search, lifted from the legacy gallery (PhotoGallery.tsx's
 * tryLocalSearch / getSemanticQueryEncoder). Answers a query entirely against
 * the locally-cached lexical + vector index instead of the slow server-side
 * /photos/search full-scan: lexical scoring (localLexicalSearch.ts, a faithful
 * port of backend search_utils.py) plus a bonus semantic/CLIP tier when a
 * vector index and the on-device text encoder are both available.
 *
 * The CLIP text encoder lives inside PhotoGallery.tsx (a very large module), so
 * it's reached through a lazy dynamic import -- the same code-splitting escape
 * hatch AppServicesProvider uses -- rather than a static import that would pull
 * the whole legacy component into every caller's bundle.
 */
import { getLocalSearchIndex, type LocalSearchIndex } from './localSearchIndex';
import { runLocalSearch, type SemanticSearchContext } from './localLexicalSearch';

type PhotoGalleryRuntime = typeof import('../components/PhotoGallery');
type BrowserAiWorkerHandle = ReturnType<PhotoGalleryRuntime['createPersistentBrowserAiWorker']>;
type BrowserAiModelState = Awaited<ReturnType<PhotoGalleryRuntime['acquireBrowserAiModel']>>;

// Matches PhotoGallery.tsx's SEMANTIC_QUERY_ENCODE_TIMEOUT_MS.
const SEMANTIC_QUERY_ENCODE_TIMEOUT_MS = 15000;

// Cached at module scope so only the FIRST semantic search in a tab session
// pays the CLIP model-load cost -- acquireBrowserAiModel's own browser-cache
// use means even that is a WASM warm-up, not a re-download, if the upload
// pipeline already loaded the same model this session.
let semanticQueryEncoderPromise: Promise<{ worker: BrowserAiWorkerHandle; modelState: BrowserAiModelState } | null> | null = null;

const getSemanticQueryEncoder = (): Promise<{ worker: BrowserAiWorkerHandle; modelState: BrowserAiModelState } | null> => {
    if (!semanticQueryEncoderPromise) {
        semanticQueryEncoderPromise = import('../components/PhotoGallery')
            .then(async (runtime) => {
                const modelState = await runtime.acquireBrowserAiModel();
                if (modelState.status !== 'available' || !modelState.manifest) {
                    return null;
                }
                return { worker: runtime.createPersistentBrowserAiWorker(), modelState };
            })
            .catch(() => null);
    }
    return semanticQueryEncoderPromise;
};

// Semantic scoring is a bonus layered on top of lexical, never a requirement:
// any failure acquiring the CLIP model or encoding the query degrades to
// lexical-only rather than breaking search.
const buildSemanticContext = async (query: string, index: LocalSearchIndex): Promise<SemanticSearchContext | undefined> => {
    if (!index.vectorIndex) {
        return undefined;
    }
    try {
        const encoder = await getSemanticQueryEncoder();
        if (!encoder) {
            return undefined;
        }
        const queryEmbedding = await encoder.worker.encodeText(query, encoder.modelState, SEMANTIC_QUERY_ENCODE_TIMEOUT_MS);
        if (queryEmbedding.length === 0) {
            return undefined;
        }
        const vectorIndex = index.vectorIndex;
        return {
            queryEmbedding,
            getEmbedding: (filename: string) => vectorIndex.embeddingsByFilename.get(filename),
        };
    } catch {
        return undefined;
    }
};

export interface LocalSearchOutcome {
    filenames: string[];
    total: number;
    /** True when the CLIP semantic tier actually contributed to scoring. */
    usedSemantic: boolean;
}

/**
 * Runs the full client-side search. Returns null to signal "fall back to the
 * server endpoint" -- either the local index isn't available yet, or local
 * scoring found nothing at all (letting the server's fallback-bucket widening
 * have a shot rather than confidently reporting zero). Mirrors the legacy
 * gallery's tryLocalSearch return contract exactly.
 */
export const runLocalSemanticSearch = async (
    query: string,
    offset: number,
    limit: number,
    captureStart: Date | null,
    captureEnd: Date | null,
): Promise<LocalSearchOutcome | null> => {
    const index = await getLocalSearchIndex();
    if (!index) {
        return null;
    }
    const semantic = await buildSemanticContext(query, index);
    const { filenames, total } = runLocalSearch(
        index.rows,
        index.peopleNameIndex,
        query,
        offset,
        limit,
        captureStart,
        captureEnd,
        semantic,
    );
    if (total === 0) {
        return null;
    }
    return { filenames, total, usedSemantic: Boolean(semantic) };
};
