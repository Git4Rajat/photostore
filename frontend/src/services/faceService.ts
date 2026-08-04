import { get, post } from './apiClient';

type PersonListResponse = {
    persons?: unknown[];
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

const listPersons = async (q?: string) => {
    const url = q && q.length > 0 ? `/api/persons?q=${encodeURIComponent(q)}` : '/api/persons';
    return await get<PersonListResponse>(url);
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

const listFaces = async () => {
    return await get<FaceListResponse>('/api/faces');
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
