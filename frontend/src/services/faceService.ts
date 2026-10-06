// people_bp moved to the dedicated `extras` container app (2026-09-17, see
// app.py's APP_ROLE=extras split) -- aliased so every call site below stays
// unchanged.
import { getExtras as get, postExtras as post } from './apiClient';

type PeoplePageRow = {
    personId?: string;
    name?: string;
    isNamed?: boolean;
    faceCount?: number;
    coverFaceId?: string;
    coverFilename?: string;
};

type PeoplePageResponse = {
    available?: boolean;
    rows?: PeoplePageRow[];
    total?: number;
    hasMore?: boolean;
    namedCount?: number;
    unnamedCount?: number;
    libraryTotal?: number;
};

type MergeListResponse = {
    merges?: unknown[];
};

type MergeResponse = {
    success?: boolean;
    personId?: string;
    mergeId?: string;
    // Identity propagation (reclaiming the target's faces from unnamed clusters)
    // now runs asynchronously on the worker; present when that job was queued.
    propagateJobId?: string | null;
    autoAssignedFaces?: number;
};

type SuggestionListResponse = {
    suggestions?: unknown[];
};

type SuggestedFace = {
    faceId: string;
    filename?: string;
    bbox?: Record<string, number>;
    imageWidth?: number;
    imageHeight?: number;
    confidence?: number;
    reviewStatus?: string;
    similarity?: number;
    currentPersonId?: string;
};

type FindFacesResponse = {
    success?: boolean;
    queued?: boolean;
    status?: string;
    propagateJobId?: string | null;
    personId?: string;
    autoAssignedFaces?: number;
    autoAssigned?: string[];
    suggestions?: SuggestedFace[];
    candidateFaces?: number;
    skipped?: string;
};

type FaceListResponse = {
    faces?: unknown[];
    total?: number;
};

type BatchDeleteFacesResponse = {
    deleted?: unknown[];
    errors?: unknown[];
    deletedPersonIds?: unknown[];
    success?: boolean;
};

type BatchDeleteResponse = {
    deletedPersonIds?: unknown[];
    errors?: unknown[];
    success?: boolean;
};

const assignUnclusteredFaces = async () => {
    return await post('/api/people/assign-unclustered', {});
};

const mapPeoplePageRow = (row: PeoplePageRow) => ({
    personId: String(row.personId || ''),
    name: typeof row.name === 'string' ? row.name : '',
    isNamed: Boolean(row.isNamed),
    faceCount: Number(row.faceCount) || 0,
    representativeFace: row.coverFaceId
        ? {
            faceId: String(row.coverFaceId),
            filename: typeof row.coverFilename === 'string' ? row.coverFilename : '',
        }
        : undefined,
});

const listPersons = async (q?: string, offset = 0, limit = 15) => {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    params.set('offset', String(offset));
    params.set('limit', String(limit));
    const res = await get<PeoplePageResponse>(`/api/persons/page?${params.toString()}`);
    if (!res?.available || !Array.isArray(res.rows)) {
        return { persons: [], total: 0 };
    }
    return {
        persons: res.rows.map(mapPeoplePageRow).filter((person) => person.personId),
        total: res.total ?? res.rows.length,
    };
};

// Cheap id+name listing (no thumbnails, no pagination) covering every person in
// the account — used where a full roster is needed regardless of what's on
// screen, e.g. a merge-target picker, as opposed to listPersons' paginated,
// thumbnail-bearing page data.
const listPersonNames = async (q?: string) => {
    const persons: ReturnType<typeof mapPeoplePageRow>[] = [];
    let offset = 0;
    const limit = 500;
    for (;;) {
        const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
        if (q) params.set('q', q);
        const res = await get<PeoplePageResponse>(`/api/persons/page?${params.toString()}`);
        if (!res?.available || !Array.isArray(res.rows) || res.rows.length === 0) {
            break;
        }
        persons.push(...res.rows.map(mapPeoplePageRow).filter((person) => person.personId));
        offset += res.rows.length;
        if (!res.hasMore || res.rows.length < limit) {
            break;
        }
    }
    return { persons, total: persons.length };
};

export interface PersonRosterEntry {
    personId: string;
    name: string;
    isNamed: boolean;
    faceCount: number;
    /** Best cover face (use /api/faces/crop/<id>); null when the cluster has no usable face. */
    coverFaceId: string | null;
}

// EVERY person in the account in one request -- no page cap (accounts have tens
// of thousands of clusters). Cheap on the server: names, counts and a cover face
// id from the in-memory face map, with no per-person lookups or thumbnail
// signing (that per-page work is what listPersons does, and why it pages).
const listAllPersons = async (): Promise<PersonRosterEntry[]> => {
    const out: PersonRosterEntry[] = [];
    let offset = 0;
    const limit = 500;
    for (;;) {
        const res = await get<PeoplePageResponse>(`/api/persons/page?offset=${offset}&limit=${limit}`);
        if (!res?.available || !Array.isArray(res.rows) || res.rows.length === 0) {
            break;
        }
        out.push(...res.rows.map((row) => ({
            personId: String(row.personId || ''),
            name: typeof row.name === 'string' ? row.name : '',
            isNamed: Boolean(row.isNamed),
            faceCount: Number(row.faceCount) || 0,
            coverFaceId: row.coverFaceId || null,
        })).filter((person) => person.personId));
        offset += res.rows.length;
        if (!res.hasMore || res.rows.length < limit) {
            break;
        }
    }
    return out;
};

const getPerson = async (personId: string) => {
    return await get(`/api/persons/${personId}`);
};

const labelPerson = async (personId: string, name: string) => {
    return await post(`/api/persons/${personId}/label`, { name });
};

const mergePersons = async (personId: string, mergeIds: string[]) => {
    return await post<MergeResponse>(`/api/persons/${personId}/merge`, { mergeIds });
};

type MergeBatchPair = { targetPersonId: string; mergeIds: string[] };

type MergeBatchResponse = {
    success?: boolean;
    results?: Array<{ targetPersonId: string; success: boolean; mergeId?: string; error?: string }>;
    // One coalesced propagation pass covers every named target in the batch,
    // instead of one job per pair — see backend merge_persons_batch.
    propagateJobId?: string | null;
    autoAssignedFaces?: number;
    targetPersonIds?: string[];
};

// Bulk-approve several merge-suggestion pairs in one request so identity
// propagation (reclaiming a named person's faces from unnamed clusters) runs
// as a single background pass instead of one job per pair. Falls back to
// concurrent individual merges if the batch endpoint isn't deployed yet.
const mergePersonsBatch = async (pairs: MergeBatchPair[]) => {
    try {
        return await post<MergeBatchResponse>('/api/persons/merge/batch', { merges: pairs });
    } catch {
        // Older deployments may not have the batch endpoint yet.
    }
    const results = await Promise.allSettled(
        pairs.map((pair) => mergePersons(pair.targetPersonId, pair.mergeIds)),
    );
    const mapped = results.map((result, index) => (
        result.status === 'fulfilled'
            ? { targetPersonId: pairs[index].targetPersonId, success: true, mergeId: result.value.mergeId }
            : { targetPersonId: pairs[index].targetPersonId, success: false, error: String(result.reason) }
    ));
    return {
        success: mapped.every((r) => r.success),
        results: mapped,
        propagateJobId: null,
        autoAssignedFaces: 0,
        targetPersonIds: [],
    } as MergeBatchResponse;
};

const undoMerge = async (mergeId: string) => {
    return await post(`/api/persons/merge/${mergeId}/undo`, {});
};

const listMerges = async () => {
    return await get<MergeListResponse>(`/api/persons/merges`);
};

const separateFace = async (personId: string, faceId: string) => {
    return await post(`/api/persons/${personId}/separate`, { faceId });
};

const confirmFace = async (personId: string, faceId: string) => {
    return await post(`/api/persons/${personId}/confirm-face`, { faceId });
};

const markNotFace = async (personId: string, faceId: string) => {
    return await post(`/api/persons/${personId}/not-face`, { faceId });
};

const deletePerson = async (personId: string) => {
    return await post(`/api/persons/${personId}/delete`, {});
};

const deletePersons = async (personIds: string[]) => {
    try {
        const result = await post<BatchDeleteResponse>('/api/persons/delete', { personIds });
        return {
            deletedPersonIds: Array.isArray(result.deletedPersonIds) ? result.deletedPersonIds.filter((id): id is string => typeof id === 'string') : [],
            errors: Array.isArray(result.errors) ? result.errors : [],
            success: result.success !== false,
        };
    } catch {
        // Older deployments may not have the batch endpoint yet; keep the UI functional during rollout.
    }
    const results = await Promise.allSettled(personIds.map((personId) => deletePerson(personId)));
    const deletedPersonIds: string[] = [];
    const errors: Array<{ personId: string; error: string }> = [];
    results.forEach((result, index) => {
        const personId = personIds[index];
        if (result.status === 'fulfilled') {
            deletedPersonIds.push(personId);
        } else {
            errors.push({ personId, error: String(result.reason) });
        }
    });
    return {
        deletedPersonIds,
        errors,
        success: errors.length === 0,
    };
};

const listFaces = async (q?: string, offset = 0, limit = 50) => {
    const params = new URLSearchParams();
    if (q) params.set('q', q);
    params.set('offset', String(offset));
    params.set('limit', String(limit));
    return await get<FaceListResponse>(`/api/faces?${params.toString()}`);
};

const deleteFaces = async (faceIds: string[]) => {
    const result = await post<BatchDeleteFacesResponse>('/api/faces/delete', { faceIds });
    return {
        deleted: Array.isArray(result.deleted) ? result.deleted.filter((id): id is string => typeof id === 'string') : [],
        deletedPersonIds: Array.isArray(result.deletedPersonIds) ? result.deletedPersonIds.filter((id): id is string => typeof id === 'string') : [],
        errors: Array.isArray(result.errors) ? result.errors : [],
        success: result.success !== false,
    };
};

const findPersonFaces = async (personId: string) => {
    return await post<FindFacesResponse>(`/api/persons/${personId}/find-faces`, {});
};

const acceptSuggestedFaces = async (personId: string, faceIds: string[]) => {
    return await post(`/api/persons/${personId}/suggested-faces/accept`, { faceIds });
};

const declineSuggestedFaces = async (personId: string, faceIds: string[]) => {
    return await post(`/api/persons/${personId}/suggested-faces/decline`, { faceIds });
};

const listSuggestions = async () => {
    return await get<SuggestionListResponse>('/api/persons/suggestions');
};

const declineSuggestion = async (sourcePersonId: string, targetPersonId: string) => {
    return await post('/api/persons/suggestions/decline', { sourcePersonId, targetPersonId });
};

export default {
    assignUnclusteredFaces,
    listPersons,
    listAllPersons,
    listPersonNames,
    getPerson,
    labelPerson,
    mergePersons,
    mergePersonsBatch,
    listMerges,
    undoMerge,
    separateFace,
    confirmFace,
    markNotFace,
    deletePerson,
    deletePersons,
    listFaces,
    deleteFaces,
    findPersonFaces,
    acceptSuggestedFaces,
    declineSuggestedFaces,
    listSuggestions,
    declineSuggestion,
};

export type { SuggestedFace, FindFacesResponse };
