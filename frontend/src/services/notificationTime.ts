// Label for a notification's age, in the viewer's locale and timezone:
// last 24h -> local time, then "Yesterday", then a date up to 30 days,
// then "N mo".
const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

export const formatNotificationTime = (timestamp: number, now: number = Date.now()): string => {
    if (!Number.isFinite(timestamp)) {
        return '';
    }
    const date = new Date(timestamp);
    const age = Math.max(0, now - timestamp);
    if (age < DAY_MS) {
        return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    }
    if (age < 2 * DAY_MS) {
        return 'Yesterday';
    }
    if (age <= 30 * DAY_MS) {
        return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    }
    const months = Math.max(1, Math.floor(age / (30 * DAY_MS)));
    return `${months} mo`;
};
