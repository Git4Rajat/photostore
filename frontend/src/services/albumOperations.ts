import { sessionScope } from './sessionScope';

/**
 * Whole-row overlay for album create/delete, sibling to pendingEdits.ts
 * (which only models scalar field patches on an *existing* row). A create
 * receipt or delete tombstone has simpler semantics than a field patch --
 * presence/absence of a row, not a value with a version floor to resolve
 * -- so this intentionally doesn't reuse pendingEdits' conflict-resolution
 * machinery, just its localStorage + sessionScope() persistence convention
 * (so cross-tab/account/library isolation is inherited for free) and its
 * `storage` event sync so another tab sees a new receipt/tombstone
 * immediately.
 *
 * See docs/audits/red-dress-album-incident-2026-10-22.md /
 * birthday-album-incident-2026-10-22.md: a stale index read raced a
 * just-created album (dropping it) and a just-deleted album's row
 * survived a reload (resurrecting it) because nothing overlaid these
 * whole-row operations on top of index reads.
 */

export type AlbumCreateReceipt = {
    id: string;
    scope: string;
    token: string;
    createdAt: number;
    /** Minimal fields needed to render the album until the index confirms it. */
    draft: { name: string; photoCount: number; coverThumbnailUrl?: string; updatedAt?: string };
};

export type AlbumDeleteTombstone = {
    id: string;
    scope: string;
    token: string;
    deletedAt: number;
};

const CREATE_PREFIX = 'photostore.album-ops.create.v1:';
const DELETE_PREFIX = 'photostore.album-ops.delete.v1:';
// Pure storage-growth bound, not a correctness mechanism: Phase A's durable
// enqueue (backend _mark_album_dirty) plus the stranded-dirty reconciler
// mean a real deletion should be reflected in the index long before this
// elapses. If it hasn't, that's an indexing bug to fix, not something this
// tombstone should paper over indefinitely.
const DELETE_TOMBSTONE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
// Shorter than the delete tombstone's TTL: a create's index convergence is
// not expected to lag anywhere near as long, and unlike the tombstone this
// is the *only* eventual cleanup for a create receipt -- reconciliation by
// presence is deliberately not safe (see reconcileAlbumCreateReceipts).
const CREATE_RECEIPT_TTL_MS = 24 * 60 * 60 * 1000;

const creates = new Map<string, Map<string, AlbumCreateReceipt>>();
const deletes = new Map<string, Map<string, AlbumDeleteTombstone>>();
// Attempt chains are memory-only: persisted tombstones remain minimal. Weak
// keys release completed chains when no pending request/map references them.
const deleteAttempts = new WeakMap<AlbumDeleteTombstone, { previous?: AlbumDeleteTombstone; failed?: boolean }>();

const createsFor = (scope: string): Map<string, AlbumCreateReceipt> => {
    if (!creates.has(scope)) {
        const loaded = new Map<string, AlbumCreateReceipt>();
        try {
            const now = Date.now();
            // Snapshot every key first: localStorage.length/key(i) are live,
            // so removing an expired key mid-loop shifts the next key into
            // the current index and incrementing i then skips it entirely
            // (see docs/audits/album-persistence-recheck2-2026-10-22.md
            // finding #1 -- a fresh receipt right after an expired one was
            // silently dropped from the in-memory map, though it remained
            // on disk).
            const keys: string[] = [];
            for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key?.startsWith(CREATE_PREFIX)) keys.push(key);
            }
            for (const key of keys) {
                const [recordScope, id] = JSON.parse(key.slice(CREATE_PREFIX.length));
                if (recordScope !== scope) continue;
                const value = localStorage.getItem(key);
                if (!value) continue;
                const receipt: AlbumCreateReceipt = JSON.parse(value);
                if (now - receipt.createdAt > CREATE_RECEIPT_TTL_MS) {
                    localStorage.removeItem(key);
                    continue;
                }
                loaded.set(id, receipt);
            }
        } catch { /* storage optional; retain in-memory only */ }
        creates.set(scope, loaded);
    }
    return creates.get(scope)!;
};

const deletesFor = (scope: string): Map<string, AlbumDeleteTombstone> => {
    if (!deletes.has(scope)) {
        const loaded = new Map<string, AlbumDeleteTombstone>();
        try {
            const now = Date.now();
            // Same snapshot-before-mutate reasoning as createsFor above.
            const keys: string[] = [];
            for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key?.startsWith(DELETE_PREFIX)) keys.push(key);
            }
            for (const key of keys) {
                const [recordScope, id] = JSON.parse(key.slice(DELETE_PREFIX.length));
                if (recordScope !== scope) continue;
                const value = localStorage.getItem(key);
                if (!value) continue;
                const tombstone: AlbumDeleteTombstone = JSON.parse(value);
                if (now - tombstone.deletedAt > DELETE_TOMBSTONE_TTL_MS) {
                    localStorage.removeItem(key);
                    continue;
                }
                loaded.set(id, tombstone);
            }
        } catch { /* storage optional; retain in-memory only */ }
        deletes.set(scope, loaded);
    }
    return deletes.get(scope)!;
};

const persistCreate = (receipt: AlbumCreateReceipt | undefined, scope: string, id: string): void => {
    const key = CREATE_PREFIX + JSON.stringify([scope, id]);
    try {
        if (receipt) localStorage.setItem(key, JSON.stringify(receipt));
        else localStorage.removeItem(key);
    } catch { /* memory fallback already updated by caller */ }
};

const persistDelete = (tombstone: AlbumDeleteTombstone | undefined, scope: string, id: string): void => {
    const key = DELETE_PREFIX + JSON.stringify([scope, id]);
    try {
        if (tombstone) localStorage.setItem(key, JSON.stringify(tombstone));
        else localStorage.removeItem(key);
    } catch { /* memory fallback already updated by caller */ }
};

export const albumOperationScope = sessionScope;

const newToken = (): string => `${Date.now()}-${Math.random().toString(36).slice(2)}`;

// Preserve sub-millisecond precision in the server's ISO timestamps. Date.parse
// alone would equate two different writes within the same millisecond.
const sourceRevision = (value?: string): string | undefined => {
    if (!value || !Number.isFinite(Date.parse(value))) return undefined;
    const fractional = /\.(\d+)/.exec(value)?.[1] ?? '';
    return new Date(value).toISOString().slice(0, 19) + '.' + fractional.padEnd(9, '0');
};

/** Capture before a source request; later local saves change the token. */
export const albumCreateReadTokens = (scope = albumOperationScope()): ReadonlyMap<string, string> =>
    new Map(Array.from(createsFor(scope), ([id, receipt]) => [id, receipt.token]));

/** Refresh existing receipts without retiring protection against older indexes. */
export const observeAlbumSourceRows = (
    rows: ReadonlyArray<AlbumCreateReceipt['draft'] & { id: string }>,
    tokens: ReadonlyMap<string, string>,
    scope = albumOperationScope(),
): void => {
    for (const row of rows) {
        if (createsFor(scope).get(row.id)?.token !== tokens.get(row.id) || !tokens.has(row.id)) continue;
        updateAlbumCreateReceipt(row.id, {
            name: row.name, photoCount: row.photoCount,
            coverThumbnailUrl: row.coverThumbnailUrl, updatedAt: row.updatedAt,
        }, scope);
    }
};

/** Call right after POST /albums succeeds, before the next loadAlbums() can race ahead of the index. */
export const beginAlbumCreate = (id: string, draft: AlbumCreateReceipt['draft']): AlbumCreateReceipt => {
    const scope = albumOperationScope();
    const receipt: AlbumCreateReceipt = { id, scope, token: newToken(), createdAt: Date.now(), draft };
    createsFor(scope).set(id, receipt);
    persistCreate(receipt, scope, id);
    return receipt;
};

/**
 * Begin an optimistic or confirmed delete so it survives racing index reads.
 * Roll back failed/unacknowledged requests with rollbackAlbumDelete. Deliberately does NOT delete
 * an existing create receipt for this id: the delete request can still
 * fail (see deleteAlbums' per-item rollback), and an irreversibly-deleted
 * receipt can't be restored -- a subsequent stale index read would then
 * drop the (never actually deleted) album again (see
 * docs/audits/album-persistence-recheck-2026-10-22.md finding #1). The
 * overlay already makes the tombstone dominate the receipt for display, so
 * leaving the receipt in place costs nothing while the tombstone stands,
 * and it still expires on its own TTL if the tombstone is later cleared.
 */
export const beginAlbumDelete = (id: string): AlbumDeleteTombstone => {
    const scope = albumOperationScope();
    const previous = deletesFor(scope).get(id);
    const tombstone: AlbumDeleteTombstone = { id, scope, token: `${Date.now()}-${Math.random().toString(36).slice(2)}`, deletedAt: Date.now() };
    deletesFor(scope).set(id, tombstone);
    persistDelete(tombstone, scope, id);
    deleteAttempts.set(tombstone, { previous });
    return tombstone;
};

/** Previous attempt in the same deletion chain, for request-local rollback snapshots. */
export const previousAlbumDeleteAttempt = (attempt: AlbumDeleteTombstone): AlbumDeleteTombstone | undefined =>
    deleteAttempts.get(attempt)?.previous;

/** Roll back only this attempt; preserve older or superseding deletion evidence. */
export const rollbackAlbumDelete = (attempt: AlbumDeleteTombstone): boolean => {
    const state = deleteAttempts.get(attempt);
    if (!state) return false;
    state.failed = true;
    const records = deletesFor(attempt.scope);
    if (records.get(attempt.id)?.token !== attempt.token) return false;
    let previous = state?.previous;
    while (previous && deleteAttempts.get(previous)?.failed) previous = deleteAttempts.get(previous)?.previous;
    if (previous) records.set(attempt.id, previous);
    else records.delete(attempt.id);
    persistDelete(previous, attempt.scope, attempt.id);
    return !previous;
};

/** Call once restore-from-trash or permanent purge has been confirmed by the backend for this id. */
export const clearAlbumDeleteTombstone = (id: string, scope = albumOperationScope()): void => {
    deletesFor(scope).delete(id);
    persistDelete(undefined, scope, id);
};

/**
 * Inject pending-create rows, merge a pending receipt's draft onto a
 * matching *existing* row, and drop pending-delete rows, before a fresh
 * index/legacy read is shown. Read-time only; never mutates the snapshot
 * it was given. Deletion dominates creation: a tombstoned id is never
 * (re)injected or merged even if a create receipt for it still exists.
 *
 * The merge (not just inject-when-missing) matters because creation and a
 * later photo-add can publish to the index separately: a stale read can
 * come back *containing* the row again, just with older field values (e.g.
 * photoCount from before the add). Without merging, that row would pass
 * through untouched and regress a confirmed save (see
 * docs/audits/album-persistence-recheck2-2026-10-22.md finding #3).
 */
export const applyAlbumOperationOverlay = <T extends { id: string; updatedAt?: string; name?: string; photoCount?: number; coverThumbnailUrl?: string }>(rows: T[], toAlbum: (receipt: AlbumCreateReceipt) => T, scope = albumOperationScope()): T[] => {
    const tombstoned = deletesFor(scope);
    const pendingCreates = createsFor(scope);
    let result = rows;
    if (tombstoned.size) {
        const filtered = result.filter((row) => !tombstoned.has(row.id));
        if (filtered.length !== result.length) result = filtered;
    }
    if (pendingCreates.size) {
        result = result.map((row) => {
            let receipt = pendingCreates.get(row.id);
            if (!receipt || tombstoned.has(row.id)) return row;
            const revision = sourceRevision(row.updatedAt);
            const floor = sourceRevision(receipt.draft.updatedAt);
            // Equal/unversioned rows are not evidence that a later save has
            // converged. A strictly newer row can advance the retained draft.
            if (revision && floor && revision > floor && typeof row.name === 'string' && typeof row.photoCount === 'number') {
                updateAlbumCreateReceipt(row.id, {
                    name: row.name, photoCount: row.photoCount,
                    coverThumbnailUrl: row.coverThumbnailUrl, updatedAt: row.updatedAt,
                }, scope);
                receipt = pendingCreates.get(row.id)!;
            }
            return {
                ...row,
                name: receipt.draft.name,
                photoCount: receipt.draft.photoCount,
                updatedAt: receipt.draft.updatedAt,
                ...(receipt.draft.coverThumbnailUrl !== undefined ? { coverThumbnailUrl: receipt.draft.coverThumbnailUrl } : {}),
            };
        });
        const present = new Set(result.map((row) => row.id));
        const missing = Array.from(pendingCreates.values()).filter((receipt) => !present.has(receipt.id) && !tombstoned.has(receipt.id));
        if (missing.length) result = [...missing.map(toAlbum), ...result];
    }
    return result;
};

/**
 * Deliberately a no-op against the receipt itself. Bare presence in any one
 * read -- even the primary indexed path -- is not strong enough evidence to
 * retire a creation receipt: without a payload version/order floor, a
 * *later* read can still return older, absent-this-row data (backend
 * replica lag, retried/reordered requests, a second tab's independent
 * fetch), and with the receipt already gone there would be nothing left to
 * re-inject the row from -- reproducing the original incident one level
 * removed (see docs/audits/album-persistence-recheck-2026-10-22.md finding
 * #3; `authoritative` alone, from the prior pass, was not sufficient).
 * Receipts are retained through source confirmation to protect against later
 * older snapshots; they are cleaned up by CREATE_RECEIPT_TTL_MS on hydration.
 * `presentIds`/`authoritative` are kept as parameters for call-site
 * compatibility and potential future version-floor evidence; retained but
 * currently unused.
 */
export const reconcileAlbumCreateReceipts = (_presentIds: ReadonlySet<string>, _authoritative = false, _scope = albumOperationScope()): void => {
    // no-op by design -- see comment above.
};

/** Keep a creation receipt's draft in sync with confirmed membership/metadata changes (e.g. a successful photo add) so reconstructing the row from the receipt alone -- while the index still lags -- doesn't regress it back to its empty-at-creation state. No-op if there's no pending receipt for this id. */
export const updateAlbumCreateReceipt = (id: string, patch: Partial<AlbumCreateReceipt['draft']>, scope = albumOperationScope()): void => {
    const receipt = createsFor(scope).get(id);
    if (!receipt) return;
    const revision = sourceRevision(patch.updatedAt);
    const floor = sourceRevision(receipt.draft.updatedAt);
    if (revision && floor && revision < floor) return;
    // An older backend may omit updatedAt; discard the old floor rather than
    // allowing a later index between the two writes to masquerade as newer.
    const updated: AlbumCreateReceipt = { ...receipt, token: newToken(), draft: { ...receipt.draft, ...patch, updatedAt: patch.updatedAt } };
    createsFor(scope).set(id, updated);
    persistCreate(updated, scope, id);
};

export const hasAlbumDeleteTombstone = (id: string, scope = albumOperationScope()): boolean => deletesFor(scope).has(id);

export const __resetAlbumOperationsForTests = (): void => {
    creates.clear(); deletes.clear();
    try {
        Object.keys(localStorage)
            .filter((key) => key.startsWith(CREATE_PREFIX) || key.startsWith(DELETE_PREFIX))
            .forEach((key) => localStorage.removeItem(key));
    } catch { /* storage may be unavailable */ }
};

if (typeof window !== 'undefined') window.addEventListener('storage', (event) => {
    if (!event.key) return;
    try {
        if (event.key.startsWith(CREATE_PREFIX)) {
            const [scope, id] = JSON.parse(event.key.slice(CREATE_PREFIX.length));
            const value = localStorage.getItem(event.key);
            if (value) createsFor(scope).set(id, JSON.parse(value));
            else createsFor(scope).delete(id);
        } else if (event.key.startsWith(DELETE_PREFIX)) {
            const [scope, id] = JSON.parse(event.key.slice(DELETE_PREFIX.length));
            const value = localStorage.getItem(event.key);
            if (value) deletesFor(scope).set(id, JSON.parse(value));
            else deletesFor(scope).delete(id);
        }
    } catch { /* ignore malformed external storage records */ }
});
