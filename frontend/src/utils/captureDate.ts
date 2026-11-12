const WALL_CLOCK = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?([+-]\d{2}:\d{2})?$/;

/**
 * A capture time as a Date whose local fields (getDate, getHours, ...) read the
 * clock where it was taken, for labels and day grouping.
 *
 * The backend sends a camera time with its own offset ("2021-09-07T23:32:34+05:30")
 * or, when the zone was never recorded, with none ("2015-05-10T22:59:21").
 * new Date() would move either into the viewer's zone and push an evening shot
 * onto the next day, out of the day the server's timeline puts it in. A UTC
 * instant (an upload time, a video stamped only in UTC) has no local clock to
 * keep, so it shows in the viewer's zone as before.
 */
export const captureWallClock = (iso: string | null | undefined): Date | null => {
    if (!iso) return null;
    const match = WALL_CLOCK.exec(iso);
    if (match && match[7] !== '+00:00' && match[7] !== '-00:00') {
        const [, y, mo, d, h, mi, s] = match.map(Number);
        return new Date(y, mo - 1, d, h, mi, s);
    }
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? null : date;
};
