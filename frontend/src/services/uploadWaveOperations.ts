import type { PersistedUploadFile, PersistedUploadSession } from '../types/browserProcessing';
import { sessionScope } from './sessionScope';
import { waveShadowEnabled } from './uploadWaveClient';

const PREFIX = 'photostore.upload.wave-operation.v1:';
type Ref = { operationId: string; scope: string; itemKey: string };
export type Operation = PersistedUploadSession & { scope: string; resumedFromSessionId?: string };
const selections = new WeakMap<File[], Ref[]>();
const unadmitted = new WeakSet<File[]>();
const operations = new Map<string, Operation>();
const outcomes = new Map<string, Set<string>>();
const key = (scope: string, id: string) => PREFIX + JSON.stringify([scope, id]);
const outcomeKey = (scope: string, id: string, item: string) => key(scope, id) + ':' + item;

const persist = (operation: Operation): boolean => {
    try { localStorage.setItem(key(operation.scope, operation.id), JSON.stringify(operation)); return true; }
    catch { return false; }
};

export const loadWaveOperation = (id: string, scope: string): Operation | null => {
    if (scope !== sessionScope()) return null;
    const cacheKey = key(scope, id);
    if (operations.has(cacheKey)) return operations.get(cacheKey)!;
    try {
        const stored = JSON.parse(localStorage.getItem(cacheKey) || 'null') as Operation | null;
        if (!stored || stored.id !== id || stored.scope !== scope || !Array.isArray(stored.files)) return null;
        operations.set(cacheKey, stored);
        return stored;
    } catch { return null; }
};

// Called at selection admission, before splitting into transport chunks. The
// complete declared manifest survives loss of the in-memory queued File objects.
export const declareWaveSelection = (files: File[]): Operation | null => {
    if (!waveShadowEnabled() || !files.length || unadmitted.has(files)) return null;
    const existing = selections.get(files);
    if (existing) return loadWaveOperation(existing[0].operationId, existing[0].scope);
    const id = crypto.randomUUID();
    const scope = sessionScope();
    const operation: Operation = { id, scope, createdAt: Date.now(), files: files.map((file, index) => ({
        key: `${id}-${index}`, name: file.name, size: file.size, type: file.type,
        lastModified: file.lastModified, uploadedBytes: 0, skippedDuplicate: false, status: 'pending',
    })) };
    // Never create a server parent that this browser cannot durably identify.
    if (!persist(operation)) { unadmitted.add(files); return null; }
    operations.set(key(scope, id), operation);
    selections.set(files, operation.files.map(file => ({ operationId: id, scope, itemKey: file.key })));
    return operation;
};

export const sliceWaveSelection = (files: File[], start: number, end?: number): File[] => {
    const slice = files.slice(start, end);
    if (unadmitted.has(files)) unadmitted.add(slice);
    const refs = selections.get(files);
    if (refs) selections.set(slice, refs.slice(start, end));
    return slice;
};

export const bindWaveChild = (files: File[], session: PersistedUploadSession): void => {
    const refs = selections.get(files);
    if (unadmitted.has(files)) { session.waveAdmissionFailed = true; return; }
    if (!refs?.length || refs.length !== session.files.length) return;
    session.waveOperationId = refs[0].operationId;
    session.waveScope = refs[0].scope;
    session.files.forEach((file, index) => { file.waveItemKey = refs[index].itemKey; });
};

export const waveParentFor = (session: PersistedUploadSession): PersistedUploadSession | null => {
    if (session.waveAdmissionFailed) return null;
    if ('scope' in session && session.scope !== sessionScope()) return null;
    if (!session.waveOperationId) return session; // pre-parent diagnostic sessions
    return loadWaveOperation(session.waveOperationId, session.waveScope || '');
};

export const recordWaveItemResolved = (session: PersistedUploadSession, file: PersistedUploadFile): void => {
    if (!session.waveOperationId || !file.waveItemKey || session.waveScope !== sessionScope()) return;
    const parent = loadWaveOperation(session.waveOperationId, session.waveScope!);
    if (!parent) return;
    const cacheKey = key(parent.scope, parent.id);
    let resolved = outcomes.get(cacheKey);
    if (!resolved) {
        resolved = new Set(parent.files.filter(item => localStorage.getItem(outcomeKey(parent.scope, parent.id, item.key)))
            .map(item => item.key));
        outcomes.set(cacheKey, resolved);
    }
    try {
        localStorage.setItem(outcomeKey(parent.scope, parent.id, file.waveItemKey), 'resolved');
        resolved.add(file.waveItemKey);
    } catch { /* a lost checkpoint must hold intake open, never infer completion */ }
};

export const waveParentResolved = (session: PersistedUploadSession): boolean => {
    if (!session.waveOperationId) return true;
    const parent = loadWaveOperation(session.waveOperationId, session.waveScope || '');
    if (!parent) return false;
    return parent.files.every(file => Boolean(localStorage.getItem(outcomeKey(parent.scope, parent.id, file.key))));
};

export const waveSelectionSession = (files: File[]): PersistedUploadSession | null => {
    const refs = selections.get(files);
    if (!refs?.length) return null;
    const parent = loadWaveOperation(refs[0].operationId, refs[0].scope);
    if (!parent) return null;
    const wanted = new Set(refs.map(ref => ref.itemKey));
    return { id: parent.id, createdAt: parent.createdAt, waveOperationId: parent.id, waveScope: parent.scope,
        files: parent.files.filter(file => wanted.has(file.key)).map(file => ({ ...file, waveItemKey: file.key })) };
};

export const waveFileBelongs = (session: PersistedUploadSession, file: PersistedUploadFile): boolean =>
    !session.waveOperationId || Boolean(file.waveItemKey?.startsWith(session.waveOperationId + '-'));

// A closed/failed intake item is immutable. An explicit retry admits its
// unfinished subset through a fresh, linked parent without changing blob keys.
export const retryWaveOperation = (session: PersistedUploadSession): PersistedUploadSession => {
    if (!waveShadowEnabled() || (session.waveScope && session.waveScope !== sessionScope())) return session;
    const unfinished = session.files.filter(file => file.status !== 'done');
    if (!unfinished.length) return session;
    const id = crypto.randomUUID();
    const scope = sessionScope();
    const parent: Operation = { id, scope, createdAt: Date.now(),
        ...(session.waveOperationId ? { resumedFromSessionId: session.waveOperationId } : {}),
        files: unfinished.map((file, index) => ({ ...file, key: `${id}-${index}`, status: 'pending' })) };
    if (!persist(parent)) return session;
    operations.set(key(scope, id), parent);
    const byOldKey = new Map(unfinished.map((file, index) => [file.key, parent.files[index].key]));
    return { ...session, waveAdmissionFailed: false, waveOperationId: id, waveScope: scope,
        files: session.files.map(file => ({ ...file, waveItemKey: byOldKey.get(file.key) || file.waveItemKey })) };
};

export const clearWaveOperationCachesForTests = () => { operations.clear(); outcomes.clear(); };
