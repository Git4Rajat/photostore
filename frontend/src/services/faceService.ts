import { get, post } from './apiClient';

type PersonListResponse = {
    persons?: unknown[];
};

type MergeListResponse = {
    merges?: unknown[];
};

type SuggestionListResponse = {
    suggestions?: unknown[];
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
    return await post(`/api/persons/${personId}/merge`, { mergeIds });
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
    listMerges,
    undoMerge,
    separateFace,
    confirmFace,
    markNotFace,
    deletePerson,
    deletePersons,
    listSuggestions,
    declineSuggestion,
};
