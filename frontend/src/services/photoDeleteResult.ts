// /photos/delete reports per-photo failures inside a successful (200) response as "<filename>: <reason>".
// Only a failed soft delete leaves a photo in the gallery; "Not found" (already gone) and a trash-index
// miss (trashed, will be repaired on retry) have both left it.
const SOFT_DELETE_FAILED = ': metadata: soft-delete failed';

export const failedDeleteIds = (requested: readonly string[], errors: readonly string[] | undefined): string[] => {
    const failed = new Set((errors ?? [])
        .filter((error) => error.endsWith(SOFT_DELETE_FAILED))
        .map((error) => error.slice(0, -SOFT_DELETE_FAILED.length)));
    return requested.filter((id) => failed.has(id));
};
