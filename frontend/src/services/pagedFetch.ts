/**
 * Fetches one page of results and, if the requested page comes back empty
 * because items were removed elsewhere (a delete/merge shrank the total
 * below this page's offset), steps back to the last real page instead of
 * returning a spuriously empty page. Extracted from the identical
 * page-correction logic duplicated between FaceClusters' loadPersons and
 * loadFaces (see FaceClusters.tsx).
 */
export async function fetchPageWithCorrection<TItem>(
    fetchPage: (offset: number, limit: number) => Promise<{ items: TItem[]; total: number }>,
    targetPage: number,
    limit: number,
): Promise<{ items: TItem[]; total: number; page: number }> {
    let effectivePage = Math.max(1, targetPage);
    let offset = (effectivePage - 1) * limit;
    let result = await fetchPage(offset, limit);
    let total = result.total;
    if (result.items.length === 0 && total > 0 && effectivePage > 1) {
        effectivePage = Math.max(1, Math.ceil(total / limit));
        offset = (effectivePage - 1) * limit;
        result = await fetchPage(offset, limit);
        total = result.total;
    }
    return { items: result.items, total, page: effectivePage };
}
