import { sessionScope } from './sessionScope';

export type EditKind = 'photo' | 'person' | 'album';
type Projection = { version?: string; value: string; readId?: string };
type Field = {
    value: unknown; token: string; saved: boolean;
    observedVersion?: string; projectionVersion?: string;
    generation?: string; generationVersion?: string;
    lastReadOrder?: number; previous?: Field; failed?: boolean; sourceConfirmed?: boolean;
    checkedProjection?: Projection; pendingProjection?: Projection;
};
type Fields = Record<string, Field>;
type Journal = Record<string, Fields>;
export type PendingEdit = { scope: string; key: string; fields: Fields; previous: Fields };
const PREFIX = 'photostore.pending-edits.v1:';
const journals = new Map<string, Journal>();
let sequence = 0;
const saves = new Map<string, Promise<unknown>>();
const photoGenerations = new Map<string, { generation: string; version?: string }>();
const freshReads = new WeakMap<ReadonlySet<string>, { id: string; order: number }>();
const readSession = Math.random().toString(36).slice(2);
let readSequence = 0;
const enrichmentCursors = new Map<string, number>();
const rowOverlays = new WeakMap<object, { scope: string; signature: string; row: object }>();
const collectionOverlays = new WeakMap<object, object[]>();

export const pendingEditScope = sessionScope;
const RECORD_PREFIX = 'photostore.pending-edits.v2:';
// Independent field records avoid a read/modify/write race over one shared document.
const recordKey = (scope: string, key: string, name: string): string =>
    RECORD_PREFIX + JSON.stringify([scope, key, name]);
const serialized = new Map<string, string>();
const validField = (field: any): field is Field => field && typeof field.token === 'string'
    && field.saved === true && Object.prototype.hasOwnProperty.call(field, 'value');
const journal = (scope: string): Journal => {
    if (!journals.has(scope)) {
        const stored: Journal = {};
        // Migrate confirmed v1 receipts once. v2 tombstones override legacy values.
        try {
            const legacy = JSON.parse(localStorage.getItem(PREFIX + scope) || '{}');
            for (const [key, fields] of Object.entries(legacy)) {
                if (!fields || typeof fields !== 'object') continue;
                for (const [name, field] of Object.entries(fields)) {
                    if (validField(field)) {
                        (stored[key] ??= {})[name] = field;
                        serialized.set(recordKey(scope, key, name), '');
                    }
                }
            }
            for (let i = 0; i < localStorage.length; i += 1) {
                const storageKey = localStorage.key(i);
                if (!storageKey?.startsWith(RECORD_PREFIX)) continue;
                const [recordScope, key, name] = JSON.parse(storageKey.slice(RECORD_PREFIX.length));
                if (recordScope !== scope) continue;
                const value = localStorage.getItem(storageKey)!;
                const field = JSON.parse(value);
                serialized.set(storageKey, value);
                if (validField(field)) (stored[key] ??= {})[name] = field;
                else if (field?.deleted && stored[key]) delete stored[key][name];
            }
        } catch { /* Storage is optional; retain the in-memory journal. */ }
        journals.set(scope, stored);
    }
    return journals.get(scope)!;
};
const persistFields = (scope: string, key: string, names: string[]): void => {
    const fields = journal(scope)[key] || {};
    for (const name of names) {
        let field: Field | undefined = fields[name];
        while (field && (!field.saved || field.failed)) field = field.previous;
        if (!field && fields[name] && !fields[name].saved && !fields[name].failed) continue;
        const storageKey = recordKey(scope, key, name);
        const value = JSON.stringify(field ? {
            value: field.value, token: field.token, saved: true,
            observedVersion: field.observedVersion, projectionVersion: field.projectionVersion,
            pendingProjection: field.pendingProjection, generation: field.generation,
            generationVersion: field.generationVersion,
            sourceConfirmed: field.sourceConfirmed, checkedProjection: field.checkedProjection,
        } : { deleted: true });
        if (serialized.get(storageKey) === value) continue;
        try { localStorage.setItem(storageKey, value); serialized.set(storageKey, value); } catch { /* memory fallback */ }
    }
};

export const beginPendingEdit = (kind: EditKind, id: string, patch: object): PendingEdit => {
    const scope = pendingEditScope();
    const key = JSON.stringify([kind, id]);
    const previous = { ...journal(scope)[key] };
    const token = `${Date.now()}-${++sequence}-${Math.random().toString(36).slice(2)}`;
    const fields = Object.fromEntries(Object.entries(patch).map(([name, value]) => [name, {
        value, token, saved: false, previous: previous[name],
        generation: kind === 'photo' ? (photoGenerations.get(JSON.stringify([scope, id]))?.generation ?? previous[name]?.generation) : undefined,
        generationVersion: kind === 'photo' ? (photoGenerations.get(JSON.stringify([scope, id]))?.version ?? previous[name]?.generationVersion) : undefined,
    }]));
    journal(scope)[key] = { ...previous, ...fields };
    return { scope, key, previous, fields };
};

export const confirmPendingEdit = (edit: PendingEdit, canonical: object = {}): void => {
    // Mutate the original fields too: a newer failed request may restore them.
    for (const [name, field] of Object.entries(edit.fields)) {
        field.saved = true;
        field.previous = undefined;
        if (Object.prototype.hasOwnProperty.call(canonical, name)) field.value = (canonical as Record<string, unknown>)[name];
    }
    persistFields(edit.scope, edit.key, Object.keys(edit.fields));
};

export const rollbackPendingEdit = (edit: PendingEdit): void => {
    Object.values(edit.fields).forEach(field => { field.failed = true; });
    const current = journal(edit.scope)[edit.key];
    if (!current) return;
    for (const [name, field] of Object.entries(edit.fields)) {
        if (current[name]?.token !== field.token) continue;
        let previous: Field | undefined = field.previous;
        while (previous?.failed) previous = previous.previous;
        if (previous) current[name] = previous; else delete current[name];
    }
    if (!Object.keys(current).length) delete journal(edit.scope)[edit.key];
    persistFields(edit.scope, edit.key, Object.keys(edit.fields));
};

export const applyPendingEdits = <T extends object>(
    kind: EditKind, id: string, row: T, version?: string, scope = pendingEditScope(),
    readTokens?: ReadonlySet<string>,
): T => {
    const fields = journal(scope)[JSON.stringify([kind, id])];
    if (!fields) return row;
    const result = { ...row } as Record<string, unknown>;
    const changed: string[] = [];
    for (const [name, field] of Object.entries(fields)) {
        const matches = JSON.stringify(result[name]) === JSON.stringify(field.value);
        const eligible = field.saved && readTokens?.has(field.token);
        if (eligible) {
            // Publication evidence is monotonic, including conflicts awaiting a source read.
            const floor = [field.projectionVersion, field.observedVersion, field.checkedProjection?.version, field.pendingProjection?.version]
                .filter(Boolean).sort().pop();
            if (version && (!floor || version >= floor)) {
                const read = readTokens && freshReads.get(readTokens);
                if (read) field.lastReadOrder = Math.max(field.lastReadOrder || 0, read.order);
                if (field.projectionVersion !== version) { field.projectionVersion = version; changed.push(name); }
                if (matches) {
                    if (field.observedVersion !== version) { field.observedVersion = version; changed.push(name); }
                    if (field.pendingProjection) changed.push(name);
                    field.pendingProjection = undefined;
                } else if (!floor || version > floor) {
                    field.pendingProjection = { version, value: JSON.stringify(result[name]) }; changed.push(name);
                }
            } else if (!version) {
                // An unversioned read (paged/fallback endpoints with no
                // sourceVersion) still must be able to raise or revalidate a
                // conflict even once a versioned claim (`floor`) is on record
                // -- otherwise a field that was ever confirmed through a
                // versioned path could never again be revalidated through an
                // unversioned one (e.g. after the primary index falls back
                // to a legacy paged/album endpoint). What it must NOT do is
                // clear a conflict a versioned read raised: it carries no
                // publication order, so it can't prove that conflict stale --
                // only a real source check (or a newer/matching versioned
                // read) may resolve it.
                const read = readTokens && freshReads.get(readTokens);
                // Only an actual fresh read can revalidate a repeated unversioned value.
                // Request order also fences late responses from earlier page requests.
                if (read ? (!field.lastReadOrder || read.order >= field.lastReadOrder) : !field.pendingProjection) {
                    if (read) field.lastReadOrder = read.order;
                    const value = JSON.stringify(result[name]);
                    if (matches) {
                        // A newer-or-equal-order read proving convergence supersedes
                        // whatever earlier unversioned evidence raised the conflict --
                        // a still in-flight source check for that earlier evidence
                        // must not later resolve against this now-stale conflict.
                        if (field.pendingProjection && field.pendingProjection.version === undefined) {
                            field.pendingProjection = undefined; changed.push(name);
                        }
                    } else if ((!field.pendingProjection || field.pendingProjection.version === undefined)
                        && (read ? field.checkedProjection?.readId !== read.id
                            : field.checkedProjection?.value !== value)) {
                        // Symmetric with the match branch above: an unversioned
                        // mismatch is weaker evidence than an existing versioned
                        // conflict and must not overwrite it either -- doing so
                        // would downgrade it to unversioned, after which a later
                        // unversioned match could wrongly clear it outright,
                        // dropping the still-outstanding versioned source check.
                        field.pendingProjection = { value, ...(read ? { readId: read.id } : {}) }; changed.push(name);
                    }
                }
            }
        }
        // Keep the receipt after convergence so an older replica cannot undo it.
        result[name] = field.value;
    }
    if (!Object.keys(fields).length) delete journal(scope)[JSON.stringify([kind, id])];
    if (changed.length) persistFields(scope, JSON.stringify([kind, id]), Array.from(new Set(changed)));
    if (Object.entries(result).every(([name, value]) => (row as Record<string, unknown>)[name] === value)) return row;
    const signature = JSON.stringify(Object.entries(fields).map(([name, field]) => [name, field.value]));
    const cached = rowOverlays.get(row);
    if (cached?.scope === scope && cached.signature === signature) return cached.row as T;
    rowOverlays.set(row, { scope, signature, row: result });
    return result as T;
};

export const applyPendingPhotoCollection = <T extends { id: string }>(rows: T[] | undefined): T[] | undefined => {
    if (!rows) return rows;
    const merged = rows.map(row => applyPendingEdits('photo', row.id, row));
    if (merged.every((row, index) => row === rows[index])) return rows;
    const cached = collectionOverlays.get(rows) as T[] | undefined;
    if (cached?.every((row, index) => row === merged[index])) return cached;
    collectionOverlays.set(rows, merged);
    return merged;
};

/** Capture before a request; cached snapshots must explicitly opt out of fresh-read identity. */
export const pendingEditReadTokens = (scope = pendingEditScope(), fresh = true): ReadonlySet<string> => {
    const tokens = new Set(Object.values(journal(scope)).flatMap(fields =>
        Object.values(fields).filter(field => field.saved).map(field => field.token)));
    if (fresh) {
        const order = ++readSequence;
        freshReads.set(tokens, { id: `${readSession}-${order}`, order });
    }
    return tokens;
};

export const hasSourceConfirmation = (kind: EditKind, id: string, name: string): boolean =>
    Boolean(journal(pendingEditScope())[JSON.stringify([kind, id])]?.[name]?.sourceConfirmed);

export const hasPendingEdits = (): boolean => Object.values(journal(pendingEditScope()))
    .some(fields => Object.values(fields).some(field => field.pendingProjection || (!field.observedVersion && !field.sourceConfirmed)));

export const pendingEntityIds = (kind: EditKind, scope = pendingEditScope()): string[] => Object.keys(journal(scope))
    .map(key => JSON.parse(key) as [EditKind, string]).filter(([type]) => type === kind).map(([, id]) => id);

/** Preserve user action order even when the network would otherwise reorder writes. */
export const savePendingEdits = <T>(edits: PendingEdit[], save: () => Promise<T>): Promise<T> => {
    const keys = Array.from(new Set(edits.map(edit => JSON.stringify([edit.scope, edit.key]))));
    const request = Promise.all(keys.map(key => saves.get(key)?.catch(() => undefined))).then(() => {
        if (edits.some(edit => edit.scope !== pendingEditScope())) {
            throw new Error('The active account or library changed before this edit could be saved.');
        }
        return save();
    });
    keys.forEach(key => saves.set(key, request));
    void request.finally(() => keys.forEach(key => { if (saves.get(key) === request) saves.delete(key); })).catch(() => undefined);
    return request;
};

export const observePhotoGeneration = (id: string, generation: string | undefined, scope = pendingEditScope(), version?: string): void => {
    if (!generation) return;
    const mapKey = JSON.stringify([scope, id]);
    const key = JSON.stringify(['photo', id]);
    const fields = journal(scope)[key];
    const known = photoGenerations.get(mapKey);
    const floor = [known?.version, ...Object.values(fields || {}).map(field => field.generationVersion)]
        .filter(Boolean).sort().pop();
    // A manifest validator is insufficient: only the payload revision proves replacement order.
    if (floor && (!version || version < floor)) return;
    if (known && known.version === version && known.generation !== generation) return;
    if (Object.values(fields || {}).some(field => field.generation && field.generation !== generation
        && field.generationVersion === version)) return;
    photoGenerations.set(mapKey, { generation, version });
    if (!fields) return;
    const changed: string[] = [];
    for (const [name, field] of Object.entries(fields)) {
        if (field.generation && field.generation !== generation) {
            if (version && field.generationVersion && version > field.generationVersion) {
                delete fields[name]; changed.push(name);
            } else if (version && !field.generationVersion) {
                // A legacy receipt has no ordered baseline; establish one conservatively.
                field.generationVersion = version; changed.push(name);
            }
        } else if (!field.generation || (version && version !== field.generationVersion)) {
            field.generation = generation; field.generationVersion = version; changed.push(name);
        }
    }
    if (!Object.keys(fields).length) delete journal(scope)[key];
    if (changed.length) persistFields(scope, key, changed);
};

export const __resetPendingEditsForTests = (): void => {
    journals.clear(); saves.clear(); photoGenerations.clear(); serialized.clear(); enrichmentCursors.clear();
    try {
        Object.keys(localStorage).filter(key => (key.startsWith(PREFIX) || key.startsWith(RECORD_PREFIX))).forEach(key => localStorage.removeItem(key));
    } catch { /* storage may be unavailable */ }
};

/** Acknowledged receipts are passive until an incoming projection conflicts. */
export type SourceEdit = { kind: EditKind; id: string; fields: Record<string, string>; projections?: Record<string, Projection>; projectionVersions?: Record<string, string | undefined>; readOrders?: Record<string, number | undefined> };
export const sourceEditRequests = (scope = pendingEditScope()): SourceEdit[] =>
    Object.entries(journal(scope)).flatMap(([key, fields]) => {
        const [kind, id] = JSON.parse(key);
        const selected = Object.entries(fields).filter(([, field]) => field.saved && !field.failed
            && ((!field.sourceConfirmed && !field.observedVersion) || field.pendingProjection));
        const tokens = Object.fromEntries(selected.map(([name, field]) => [name, field.token]));
        const projections = Object.fromEntries(selected.filter(([, field]) => field.pendingProjection)
            .map(([name, field]) => [name, field.pendingProjection!]));
        const projectionVersions = Object.fromEntries(selected.map(([name, field]) => [name, field.projectionVersion]));
        // A later read -- even one that merely confirms a match, with no
        // projection to show for it -- can still prove this request's
        // resolution stale by the time it returns. Only initial, no-conflict
        // checks need this: a projection-bearing conflict already fences via
        // pendingProjection/projectionVersion equality, and unlike
        // lastReadOrder (session-only, never persisted) those survive reload,
        // so leaving this unset for conflicts keeps that durability intact.
        const readOrders = Object.fromEntries(selected.filter(([, field]) => !field.pendingProjection)
            .map(([name, field]) => [name, field.lastReadOrder]));
        return selected.length ? [{ kind, id, fields: tokens, projections, projectionVersions, readOrders }] : [];
    });

export const pendingProjectionEntityIds = (kind: EditKind, scope = pendingEditScope()): string[] =>
    Object.entries(journal(scope)).filter(([key, fields]) => JSON.parse(key)[0] === kind
        && Object.values(fields).some(field => !field.observedVersion))
        .map(([key]) => JSON.parse(key)[1]);

/** Query first, then spend the bounded enrichment budget; rotate broad reads fairly. */
export const pendingPeopleEnrichmentIds = (present: ReadonlySet<string>, query: string, scope = pendingEditScope(), limit = 100): string[] => {
    const needle = query.trim().toLowerCase();
    const candidates = pendingProjectionEntityIds('person', scope).filter(id => {
        if (present.has(id)) return false;
        const name = journal(scope)[JSON.stringify(['person', id])]?.name?.value;
        return !needle || (typeof name === 'string' && name.toLowerCase().includes(needle));
    });
    const key = JSON.stringify([scope, needle]);
    const start = (enrichmentCursors.get(key) || 0) % (candidates.length || 1);
    const selected = [...candidates.slice(start), ...candidates.slice(0, start)].slice(0, limit);
    enrichmentCursors.set(key, start + selected.length);
    return selected;
};

export const acceptSourceEdits = (requests: SourceEdit[], rows: Array<{ kind: EditKind; id: string; values: Record<string, unknown>; deleted?: boolean }>, scope: string): boolean => {
    if (scope !== pendingEditScope()) return false;
    let visibleChange = false;
    for (const row of rows) {
        const request = requests.find(item => item.kind === row.kind && item.id === row.id);
        if (!request) continue;
        const key = JSON.stringify([row.kind, row.id]);
        const fields = journal(scope)[key];
        if (!fields) continue;
        const changed: string[] = [];
        for (const [name, token] of Object.entries(request.fields)) {
            const field = fields[name];
            if (!field?.saved || field.token !== token) continue;
            // A check begun before a later projection must not resolve that newer conflict.
            if (field.projectionVersion !== request.projectionVersions?.[name]) continue;
            if (JSON.stringify(field.pendingProjection) !== JSON.stringify(request.projections?.[name])) continue;
            // An initial, no-conflict check (no projection to compare) has no
            // other durable evidence to fence against -- a read that landed
            // after it was sent, even one that only confirmed a match and
            // left no projection behind, can still mean this response is
            // answering a question that no longer needs asking. Projection-
            // bearing requests don't need this: their pendingProjection/
            // projectionVersion equality above already surfaces newer
            // evidence, and unlike lastReadOrder those persist across reload.
            if (!request.projections?.[name] && field.lastReadOrder !== request.readOrders?.[name]) continue;
            if (row.deleted) { delete fields[name]; changed.push(name); visibleChange = true; continue; }
            if (!Object.prototype.hasOwnProperty.call(row.values, name)) continue;
            if (JSON.stringify(field.value) !== JSON.stringify(row.values[name])) {
                field.observedVersion = undefined; visibleChange = true;
            }
            field.value = row.values[name];
            field.sourceConfirmed = true;
            field.checkedProjection = field.pendingProjection;
            field.pendingProjection = undefined;
            changed.push(name);
        }
        if (!Object.keys(fields).length) delete journal(scope)[key];
        if (changed.length) persistFields(scope, key, changed);
    }
    return visibleChange;
};

if (typeof window !== 'undefined') window.addEventListener('storage', event => {
    if (!event.key?.startsWith(RECORD_PREFIX)) return;
    try {
        const [scope, key, name] = JSON.parse(event.key.slice(RECORD_PREFIX.length));
        // Read current storage rather than replaying a potentially superseded event value.
        const value = localStorage.getItem(event.key);
        if (value === null) return;
        const incoming = JSON.parse(value);
        if (!validField(incoming) && !incoming?.deleted) return;
        const fields = (journal(scope)[key] ??= {});
        const current = fields[name];
        if (current && !current.saved && !current.failed) {
            // Keep optimistic fields attached to their request; rollback uses the latest receipt.
            let oldest = current;
            while (oldest.previous && !oldest.previous.saved) oldest = oldest.previous;
            oldest.previous = validField(incoming) ? incoming : undefined;
        } else if (validField(incoming)) fields[name] = incoming;
        else delete fields[name];
        serialized.set(event.key, value);
    } catch { /* Ignore malformed external storage records. */ }
});
