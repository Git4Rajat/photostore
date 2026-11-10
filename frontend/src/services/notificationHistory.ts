import type { AppNotification } from '../types/browserProcessing';
import { createBrowserDb, requestResult } from './browserStore';

// Persists the notification panel across reloads. The panel itself stays
// in-memory state in AppServicesProvider; this store only mirrors it, scoped by
// sessionScope() so one account/library never reads another's history.
//
// `action` (Undo/Retry closures) is deliberately not persisted -- it can't
// survive a reload and would render as a dead button.
const NOTIFICATION_HISTORY_DB = 'photostore-notification-history';
export const MAX_NOTIFICATION_HISTORY = 200;
const NOTIFICATION_HISTORY_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

const STORE = 'notifications';

type StoredNotification = Omit<AppNotification, 'action'> & { scope: string };

const db = createBrowserDb(NOTIFICATION_HISTORY_DB, 1, [
    { name: STORE, keyPath: 'id', indexes: [{ name: 'scope', keyPath: 'scope' }] },
]);

const toStored = (scope: string, n: AppNotification): StoredNotification => ({
    id: n.id,
    title: n.title,
    details: n.details,
    timestamp: n.timestamp,
    unread: n.unread,
    progress: n.progress,
    uploadSessionId: n.uploadSessionId,
    scope,
});

const toNotification = ({ scope: _scope, ...rest }: StoredNotification): AppNotification => rest;

export const loadNotificationHistory = async (scope: string, now = Date.now()): Promise<AppNotification[]> => {
    const rows = await db.run<StoredNotification[]>(STORE, 'readonly', (store) => (
        requestResult(store.index('scope').getAll(scope) as IDBRequest<StoredNotification[]>)
    ), []);
    return rows
        .filter((row) => now - row.timestamp <= NOTIFICATION_HISTORY_MAX_AGE_MS)
        .sort((a, b) => b.timestamp - a.timestamp)
        .slice(0, MAX_NOTIFICATION_HISTORY)
        .map(toNotification);
};

// Replaces this scope's rows with `notifications` in one transaction (<= a few
// hundred small rows, so a rewrite is simpler than diffing).
export const saveNotificationHistory = async (scope: string, notifications: AppNotification[]): Promise<void> => {
    const keep = notifications.slice(0, MAX_NOTIFICATION_HISTORY);
    await db.run<void>(STORE, 'readwrite', async (store) => {
        const existing = await requestResult(store.index('scope').getAllKeys(scope));
        const keepIds = new Set(keep.map((n) => n.id));
        for (const key of existing) {
            if (!keepIds.has(String(key))) {
                store.delete(key);
            }
        }
        for (const notification of keep) {
            store.put(toStored(scope, notification));
        }
    }, undefined);
};

// Anything that never rendered in the panel but is older than the retention
// window (other scopes included) is dropped so abandoned accounts don't
// accumulate rows forever.
export const pruneNotificationHistory = async (now = Date.now()): Promise<void> => {
    await db.run<void>(STORE, 'readwrite', async (store) => {
        const rows = await requestResult(store.getAll() as IDBRequest<StoredNotification[]>);
        for (const row of rows) {
            if (now - row.timestamp > NOTIFICATION_HISTORY_MAX_AGE_MS) {
                store.delete(row.id);
            }
        }
    }, undefined);
};

// Merge persisted rows under the live list: live entries win on id (they carry
// the freshest state and any action), history fills in the rest.
export const mergeNotificationHistory = (
    live: AppNotification[],
    history: AppNotification[],
): AppNotification[] => {
    const liveIds = new Set(live.map((n) => n.id));
    return [...live, ...history.filter((n) => !liveIds.has(n.id))]
        .sort((a, b) => b.timestamp - a.timestamp)
        .slice(0, MAX_NOTIFICATION_HISTORY);
};
