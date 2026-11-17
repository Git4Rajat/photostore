import { plural } from '../../../utils/format';

/** A failed file's stored error, in everyday words. Unknown errors keep their own text. */
export const plainFailureReason = (error?: string): string => {
    const text = (error ?? '').trim();
    if (!text) return 'Something went wrong';
    if (/network|connection|offline|timed? ?out|timeout|failed to fetch|abort/i.test(text)) return 'The connection dropped';
    if (/\b(429|5\d\d)\b|busy|unavailable|too many|server/i.test(text)) return 'The server was busy';
    if (/read|decode|corrupt|not found|permission|no longer|missing|couldn.?t find/i.test(text)) return 'This file couldn’t be read';
    return text.length > 80 ? 'Something went wrong' : text;
};

/** "About 12 minutes left", from how long the files added so far took. Null until there is enough to go on. */
export const timeLeftLabel = (elapsedMs: number, added: number, remaining: number): string | null => {
    if (added < 1 || elapsedMs < 8000 || remaining <= 0) return null;
    const seconds = (elapsedMs / added) * remaining / 1000;
    if (seconds < 60) return 'Less than a minute left';
    const minutes = Math.round(seconds / 60);
    if (minutes < 90) return `About ${plural(minutes, 'minute')} left`;
    const hours = Math.round(minutes / 6) / 10;
    return `About ${hours} hours left`;
};
