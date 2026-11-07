/** The static share page (see backend/share_snapshot_page.py) hands over to the real album page with
 *  `?photo=<index>` so the photo the visitor was looking at opens again. Returns that index when it is
 *  one of the `loaded` photos, else null (the page then just shows the grid). */
export const photoFromDeepLink = (search: string, loaded: number): number | null => {
    const raw = new URLSearchParams(search).get('photo');
    if (raw === null || !/^\d+$/.test(raw)) {
        return null;
    }
    const index = Number(raw);
    return index < loaded ? index : null;
};
