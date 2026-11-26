// Upload sessions this browser opened. The processing dock uses them to tell its own paused upload
// from one running on another device (the server lists every open session in the library).
const KEY = 'keepsake.ownUploadSessions';
const MAX = 50;

const read = (): string[] => {
    try {
        const parsed = JSON.parse(window.sessionStorage.getItem(KEY) || '[]');
        return Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === 'string') : [];
    } catch {
        return [];
    }
};

let memory: string[] = [];

export const rememberOwnUploadSession = (sessionId: string): void => {
    if (!sessionId || memory.includes(sessionId)) return;
    memory = [...memory, sessionId].slice(-MAX);
    try {
        window.sessionStorage.setItem(KEY, JSON.stringify(Array.from(new Set(read().concat(sessionId))).slice(-MAX)));
    } catch {
        // Memory alone covers this page load.
    }
};

/** True when every listed session was opened by this browser. */
export const allSessionsOwn = (sessionIds: string[] | undefined): boolean => {
    if (!sessionIds || sessionIds.length === 0) return false;
    const own = new Set<string>(memory.concat(read()));
    return sessionIds.every((id) => own.has(id));
};
