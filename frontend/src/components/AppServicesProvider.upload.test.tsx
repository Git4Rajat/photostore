import React from 'react';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    getUpload: vi.fn(),
    postUpload: vi.fn(),
    staged: vi.fn(),
    committed: vi.fn(),
    idbDelete: vi.fn(),
    idbGet: vi.fn(),
    toast: vi.fn(),
    span: vi.fn(),
    preparationEnabled: false,
    prepared: vi.fn(),
    pollAuthenticated: false,
    jobs: vi.fn(),
}));

vi.mock('../config/appConfig', () => ({ getRuntimeConfig: () => ({ processingMode: 'backend', browserUploadPreparation: mocks.preparationEnabled }) }));
vi.mock('../services/uploadPreparationPipeline', () => ({UploadPreparationPipeline: class {
    start = mocks.prepared;
    dispose() {}
}}));
vi.mock('../services/apiClient', () => ({
    get: vi.fn().mockResolvedValue({}),
    post: vi.fn().mockResolvedValue({}),
    postTools: vi.fn().mockResolvedValue({}),
    getUpload: mocks.getUpload,
    postUpload: mocks.postUpload,
    resolveApiUrl: (url: string) => url,
}));
vi.mock('../services/authClient', () => ({ isAuthEnabled: () => false, getAccessToken: async () => null }));
vi.mock('../services/jobNotifications', async (importOriginal) => ({
    ...await importOriginal<typeof import('../services/jobNotifications')>(),
    isLikelyAuthenticated: () => mocks.pollAuthenticated,
    fetchJobStatuses: mocks.jobs,
    requestJobPoll: vi.fn(),
}));
vi.mock('../services/backgroundKeepAlive', () => ({
    BackgroundKeepAlive: class {
        start() {}
        stop() {}
        dispose() {}
    },
}));
vi.mock('../services/toast', () => ({ showToast: mocks.toast }));
vi.mock('../services/perf', () => ({ perf: { recordSpan: mocks.span } }));
vi.mock('./PhotoGallery', () => ({
    idbPut: vi.fn().mockResolvedValue(undefined),
    idbGet: mocks.idbGet,
    idbDelete: mocks.idbDelete,
    readBlobArrayBuffer: async (file: File) => new TextEncoder().encode(file.name).buffer,
    sha256ArrayBuffer: async (buffer: ArrayBuffer) => Array.from(new Uint8Array(buffer)).join('-'),
    createBrowserThumbnail: vi.fn().mockResolvedValue(null),
}));
vi.mock('@azure/storage-blob', () => ({
    BlockBlobClient: class {
        stageBlock = mocks.staged;
        commitBlockList = mocks.committed;
    },
}));

import { AppServicesProvider, useAppServices } from './AppServicesProvider';
import { UPLOAD_SESSION_STORAGE_KEY } from './browserAiShared';

let services: ReturnType<typeof useAppServices>;
function Probe() {
    services = useAppServices();
    return <button onClick={services.requestUpload}>Upload</button>;
}

const filesFor = (count: number) => Array.from({ length: count }, (_, index) => (
    new File([new Uint8Array([index % 256, Math.floor(index / 256)])], `original-${index}.heic`, {
        type: 'image/heic', lastModified: 1700000000000 + index,
    })
));

const initFiles = () => mocks.postUpload.mock.calls
    .filter(([url]) => url === '/upload/init-batch')
    .flatMap(([, payload]) => payload.files);
const finalizedFiles = () => mocks.postUpload.mock.calls
    .filter(([url]) => url === '/upload/finalize-batch')
    .flatMap(([, payload]) => payload.files);
const refreshRequests = () => mocks.postUpload.mock.calls
    .filter(([url]) => url === '/upload/known-hashes/refresh');

function memoryStorage(): Storage {
    const items = new Map<string, string>();
    return {
        get length() { return items.size; },
        clear: () => items.clear(),
        getItem: (key) => items.get(key) ?? null,
        setItem: (key, value) => { items.set(key, value); },
        removeItem: (key) => { items.delete(key); },
        key: (index) => Array.from(items.keys())[index] ?? null,
    };
}

async function advance(ms: number) {
    await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mocks.preparationEnabled = false;
    mocks.pollAuthenticated = false;
    mocks.jobs.mockResolvedValue([]);
    mocks.prepared.mockResolvedValue({});
    vi.stubGlobal('localStorage', memoryStorage());
    vi.stubGlobal('sessionStorage', memoryStorage());
    localStorage.clear();
    sessionStorage.clear();
    mocks.getUpload.mockImplementation(async () => ({ hashes: {} }));
    mocks.staged.mockResolvedValue(undefined);
    mocks.committed.mockResolvedValue(undefined);
    mocks.idbDelete.mockResolvedValue(undefined);
    mocks.idbGet.mockResolvedValue(null);
    mocks.postUpload.mockImplementation(async (url: string, payload: { filename: string; files: Array<{ filename: string }> }) => {
        if (url === '/upload/init') {
            return { uploadId: payload.filename, blobName: `blob-${payload.filename}`,
                blobUrl: `https://test.blob.core.windows.net/images/${payload.filename}` };
        }
        if (url === '/upload/init-batch') {
            return { results: payload.files.map((file) => ({
                uploadId: file.filename,
                blobName: `blob-${file.filename}`,
                blobUrl: `https://test.blob.core.windows.net/images/${file.filename}`,
                uploadPreparationEnabled: true,
                previewBlobUrl: 'https://test.blob.core.windows.net/thumbnails/preview.jpg',
                metadataBlobUrl: 'https://test.blob.core.windows.net/thumbnails/metadata.bin',
            })) };
        }
        if (url === '/upload/finalize-batch') {
            return { results: payload.files.map((file) => ({ filename: file.filename, complete: true, duplicates: [] })) };
        }
        return {};
    });
});

afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
});

function select(files: File[]) {
    const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
    fireEvent.click(view.getByText('Upload'));
    const input = view.container.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files } });
    return input;
}

describe('index completion notifications', () => {
    const indexJob = (scope: string, message: string, millisecondsAgo = 0) => ({
        jobId: `index-build-owner-${scope}`, kind: 'index_build', status: 'done', indexScope: scope,
        title: scope.startsWith('dedup') ? 'Duplicate-check index updated' : `${scope} index updated`,
        message, updatedAt: new Date(Date.now() - millisecondsAgo).toISOString(),
    });
    const focus = async () => {
        await act(async () => { window.dispatchEvent(new Event('focus')); });
        await advance(0);
    };

    it('distinguishes index categories and keeps only the newest successful duplicate-check pass', async () => {
        mocks.pollAuthenticated = true;
        mocks.jobs.mockResolvedValue([
            indexJob('dedup-idle', 'Newest duplicate-check snapshot'),
            indexJob('dedup', 'Older duplicate-check snapshot', 1000),
            indexJob('light', 'Photo ordering and access'),
            indexJob('people', 'People and album listings'),
            indexJob('full', 'Full library indexes'),
        ]);
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        expect(services.notifications).toHaveLength(4);
        expect(services.notifications.find(n => n.title === 'Duplicate-check index updated')?.details).toBe('Newest duplicate-check snapshot');
        expect(new Set(services.notifications.map(n => n.title)).size).toBe(4);
        expect(mocks.toast).not.toHaveBeenCalled();
    });

    it('updates one entry for later batches with the same job ID without repeating the same completion', async () => {
        mocks.pollAuthenticated = true;
        const first = indexJob('light', 'First batch');
        mocks.jobs.mockResolvedValue([first]);
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        const id = services.notifications[0].id;
        await advance(2000);
        mocks.jobs.mockResolvedValue([{...first, message: 'Second batch', updatedAt: new Date().toISOString()}]);
        await focus();
        expect(services.notifications).toHaveLength(1);
        expect(services.notifications[0]).toMatchObject({id, details: 'Second batch'});
        act(() => services.markAllNotificationsRead());
        await focus();
        expect(services.notifications[0].unread).toBe(false);
        act(() => services.clearNotifications());
        await advance(2000);
        mocks.jobs.mockResolvedValue([{...first, message: 'Third batch', updatedAt: new Date().toISOString()}]);
        await focus();
        expect(services.notifications).toHaveLength(1);
        expect(services.notifications[0].details).toBe('Third batch');
    });

    it('does not hide a failed maintenance pass behind another successful update', async () => {
        mocks.pollAuthenticated = true;
        mocks.jobs.mockResolvedValue([
            indexJob('dedup-idle', 'Snapshot refreshed'),
            {...indexJob('dedup', 'Storage unavailable', 1000), status: 'failed', title: 'Duplicate-check index update failed'},
        ]);
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        expect(services.notifications).toHaveLength(2);
        expect(services.notifications.some(n => n.details === 'Storage unavailable')).toBe(true);
    });

    it('separates server photo processing completion from indexing and people grouping', async () => {
        mocks.pollAuthenticated = true;
        const running = {jobId: 'photo-job', kind: 'ipwork', status: 'running', title: '', message: ''};
        mocks.jobs.mockResolvedValue([running]);
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        expect(services.notifications.some(n => n.title === 'Server processing started')).toBe(true);
        mocks.jobs.mockResolvedValue([{...running, status: 'done'}]);
        await advance(6000);
        await advance(40000);
        expect(services.notifications.find(n => n.title === 'Server photo processing finished')?.details)
            .toContain('Index updates and people grouping are reported separately');
    });
});

describe('browser upload selection and startup', () => {
    it('starts original transfers without waiting for preparation, then finalizes its assets in backend mode', async () => {
        mocks.preparationEnabled = true;
        let resolvePreparation!: (value: unknown) => void;
        mocks.prepared.mockImplementation(() => new Promise(resolve => { resolvePreparation = resolve; }));
        select(filesFor(1));
        await advance(1000);
        expect(mocks.staged).toHaveBeenCalledTimes(1);
        expect(mocks.committed).toHaveBeenCalledTimes(1);
        expect(finalizedFiles()).toHaveLength(0);
        expect(initFiles()[0]).toMatchObject({includePreview: true, includeMetadata: true});
        const prepared = {clientProcessing: {preview: {alreadyUploaded: true}},
            clientMetadata: {version: 1, bytes: 400, sha256: 'b'.repeat(64), sourceSha256: 'a'.repeat(64)},
            preparationReport: {version: 1, elapsedMs: 200, assets: {preview: {target: true, generation: 'generated', upload: 'uploaded'}}}};
        await act(async () => { resolvePreparation(prepared); });
        await advance(4000);
        expect(finalizedFiles()[0]).toMatchObject({...prepared, blobName: 'blob-original-0.heic'});
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('does not request or run preparation when the feature is off', async () => {
        select(filesFor(1));
        await advance(4000);
        expect(initFiles()[0].includePreview).toBeUndefined();
        expect(mocks.prepared).not.toHaveBeenCalled();
        expect(finalizedFiles()).toHaveLength(1);
    });

    it.each([300, 3000])('initializes and finalizes all %i selected originals', async (count) => {
        const files = filesFor(count);
        const writes = vi.spyOn(localStorage, 'setItem');
        select(files);
        for (let tick = 0; tick < 150 && finalizedFiles().length < count; tick += 1) {
            await advance(1000);
        }
        await advance(1000);
        expect(initFiles().map((file) => file.filename).sort()).toEqual(files.map((file) => file.name).sort());
        expect(finalizedFiles()).toHaveLength(count);
        expect(new Set(finalizedFiles().map((file) => file.filename)).size).toBe(count);
        expect(finalizedFiles().every((file) => file.totalSize === 2 && file.contentType === 'image/heic')).toBe(true);
        expect(mocks.staged).toHaveBeenCalledTimes(count);
        expect(refreshRequests()).toHaveLength(1);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'completed' });
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.queuedUploadFileCount).toBe(0);
        expect(writes.mock.calls.filter(([key]) => key === UPLOAD_SESSION_STORAGE_KEY).length).toBeLessThan(count / 10);
        expect(mocks.span).toHaveBeenCalledWith('upload.picker.change', expect.any(Number), {
            rows: count, bytes: count * 2,
        });
    });

    it('starts transfers when the optional known-hash request never settles', async () => {
        mocks.getUpload.mockImplementation((url: string) => url === '/upload/known-hashes'
            ? new Promise(() => {}) : Promise.resolve({}));
        select(filesFor(3));
        await advance(4000);
        expect(initFiles()).toHaveLength(3);
        expect(finalizedFiles()).toHaveLength(3);
        expect(mocks.staged).toHaveBeenCalledTimes(3);
    });

    it('starts immediately and uses a slow hash download to skip remaining files in the same upload', async () => {
        const files = filesFor(50);
        let resolveHashes!: (value: unknown) => void;
        let releaseTransfers!: () => void;
        mocks.getUpload.mockImplementation((url: string) => url === '/upload/known-hashes'
            ? new Promise((resolve) => { resolveHashes = resolve; }) : Promise.resolve({}));
        const transferGate = new Promise<void>((resolve) => { releaseTransfers = resolve; });
        mocks.staged.mockImplementation(() => transferGate);
        const input = select(files);
        await advance(1000);
        const transfersStarted = mocks.staged.mock.calls.length;
        expect(transfersStarted).toBeGreaterThan(0);
        expect(transfersStarted).toBeLessThan(files.length);
        await advance(9000);
        const hashConfig = mocks.getUpload.mock.calls.find(([url]) => url === '/upload/known-hashes')![1];
        fireEvent(input, new Event('cancel'));
        expect(hashConfig.signal.aborted).toBe(false);
        await act(async () => {
            resolveHashes({ hashes: Object.fromEntries(files.map((file) => [
                Array.from(new TextEncoder().encode(file.name)).join('-'), `stored-${file.name}`,
            ])) });
        });
        await act(async () => { releaseTransfers(); });
        await advance(3000);
        expect(mocks.staged).toHaveBeenCalledTimes(transfersStarted);
        expect(finalizedFiles()).toHaveLength(transfersStarted);
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.notifications.some((notification) =>
            notification.progress?.skippedDuplicateCount === files.length - transfersStarted)).toBe(true);
        expect(mocks.getUpload.mock.calls.filter(([url]) => url === '/upload/known-hashes')).toHaveLength(1);
    });

    it('refreshes after a run ends with failed files', async () => {
        mocks.staged.mockRejectedValue(new Error('Test transfer failure'));
        select(filesFor(2));
        await advance(20000);
        expect(services.pendingUploadSummary?.fileCount).toBe(2);
        expect(refreshRequests()).toHaveLength(1);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'failed' });
    });

    it('refreshes after stopping an active upload', async () => {
        mocks.staged.mockImplementation((_id, _data, _size, options) => new Promise((_resolve, reject) => {
            const abort = () => reject(new Error('upload_stopped_by_user'));
            if (options.abortSignal.aborted) abort();
            else options.abortSignal.addEventListener('abort', abort, { once: true });
        }));
        select(filesFor(50));
        await advance(1000);
        expect(services.uploading).toBe(true);
        expect(refreshRequests()).toHaveLength(0);
        act(() => { services.stopActiveUpload(); });
        await advance(3000);
        expect(services.uploading).toBe(false);
        expect(refreshRequests()).toHaveLength(1);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'stopped' });
    });

    it('includes late finalization when stopping the remaining transfers', async () => {
        let releaseFinalize!: () => void;
        const finalizeGate = new Promise<void>((resolve) => { releaseFinalize = resolve; });
        const originalPost = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url, payload) => {
            if (url === '/upload/finalize-batch') await finalizeGate;
            return originalPost(url, payload);
        });
        mocks.staged.mockResolvedValueOnce(undefined).mockImplementation((_id, _data, _size, options) =>
            new Promise((_resolve, reject) => {
                const abort = () => reject(new Error('upload_stopped_by_user'));
                if (options.abortSignal.aborted) abort();
                else options.abortSignal.addEventListener('abort', abort, { once: true });
            }));
        select(filesFor(50));
        await advance(1000);
        expect(finalizedFiles()).toHaveLength(1);
        act(() => { services.stopActiveUpload(); });
        await advance(3000);
        expect(services.uploading).toBe(false);
        expect(refreshRequests()).toHaveLength(0);
        await act(async () => { releaseFinalize(); });
        await advance(1000);
        expect(refreshRequests()).toHaveLength(1);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'stopped' });
    });

    it('does not refresh before pending finalization has finished', async () => {
        let releaseFinalize!: () => void;
        const finalizeGate = new Promise<void>((resolve) => { releaseFinalize = resolve; });
        const originalPost = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url, payload) => {
            if (url === '/upload/finalize-batch') await finalizeGate;
            return originalPost(url, payload);
        });
        select(filesFor(3));
        await advance(1000);
        expect(finalizedFiles()).toHaveLength(3);
        expect(refreshRequests()).toHaveLength(0);
        await act(async () => { releaseFinalize(); });
        await advance(3000);
        expect(refreshRequests()).toHaveLength(1);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'completed' });
    });

    it('saves finalized recovery state before deleting cached source bytes', async () => {
        let releaseFinalize!: () => void;
        const gate = new Promise<void>((resolve) => { releaseFinalize = resolve; });
        const originalPost = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url, payload) => {
            if (url === '/upload/finalize-batch') await gate;
            return originalPost(url, payload);
        });
        const deletedStates: string[] = [];
        mocks.idbDelete.mockImplementation(async (key) => {
            const saved = JSON.parse(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)!);
            deletedStates.push(saved.files.find((file: { key: string }) => file.key === key).status);
        });
        select(filesFor(3));
        await advance(1000);
        expect(mocks.idbDelete).not.toHaveBeenCalled();
        await act(async () => { releaseFinalize(); });
        await advance(1000);
        expect(mocks.idbDelete).not.toHaveBeenCalled();
        await advance(2000);
        expect(deletedStates.length).toBeGreaterThanOrEqual(3);
        expect(deletedStates.every((status) => status === 'done')).toBe(true);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'completed' });
    });

    it('flushes active upload progress when the page is hidden or unloaded', async () => {
        mocks.staged.mockImplementation((_id, _data, _size, options) => new Promise((_resolve, reject) => {
            const abort = () => reject(new Error('upload_stopped_by_user'));
            if (options.abortSignal.aborted) abort();
            else options.abortSignal.addEventListener('abort', abort, { once: true });
        }));
        select(filesFor(3));
        await advance(1000);
        const before = JSON.parse(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)!);
        expect(before.files.every((file: { uploadId?: string }) => !file.uploadId)).toBe(true);
        vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
        fireEvent(document, new Event('visibilitychange'));
        const hidden = JSON.parse(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)!);
        expect(hidden.files.every((file: { uploadId?: string }) => !!file.uploadId)).toBe(true);
        fireEvent(window, new Event('pagehide'));
        fireEvent(window, new Event('beforeunload'));
        expect(services.uploading).toBe(true);
        act(() => { services.stopActiveUpload(); });
        await advance(20000);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
        await advance(3000);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
    });

    it('does not slow duplicate-only selections by waiting for a checkpoint in each lane', async () => {
        const files = filesFor(3000);
        const skippedKeys = new Set<string>();
        mocks.idbDelete.mockImplementation(async (key) => {
            const saved = JSON.parse(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)!);
            const file = saved.files.find((item: { key: string }) => item.key === key);
            expect(file).toMatchObject({ status: 'done', skippedDuplicate: true });
            skippedKeys.add(key);
        });
        mocks.getUpload.mockResolvedValue({ hashes: Object.fromEntries(files.map((file) => [
            Array.from(new TextEncoder().encode(file.name)).join('-'), `stored-${file.name}`,
        ])) });
        select(files);
        await advance(1000);
        expect(mocks.staged).not.toHaveBeenCalled();
        expect(refreshRequests()[0][1]).toEqual({ reason: 'completed' });
        expect(skippedKeys.size).toBe(3000);
        expect(services.queuedUploadFileCount).toBe(0);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
    });

    it('restores the existing checkpoint format and reuses already staged blocks after reload', async () => {
        const file = filesFor(1)[0];
        mocks.idbGet.mockResolvedValue(file);
        const blockId = btoa('block-00000000');
        localStorage.setItem(UPLOAD_SESSION_STORAGE_KEY, JSON.stringify({
            id: 'legacy', createdAt: Date.now(), files: [{
                key: 'legacy:0', name: file.name, size: file.size, type: file.type,
                lastModified: file.lastModified, uploadedBytes: file.size, skippedDuplicate: false,
                status: 'finalizing', uploadId: file.name, blobName: `blob-${file.name}`,
                blockIds: [blockId], chunkSizeBytes: file.size,
            }],
        }));
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(4000);
        expect(mocks.staged).not.toHaveBeenCalled();
        expect(mocks.committed).toHaveBeenCalledWith([blockId], expect.anything());
        expect(finalizedFiles()).toHaveLength(1);
        expect(mocks.postUpload.mock.calls.find(([url]) => url === '/upload/init')![1]).toMatchObject({ uploadId: file.name });
        expect(services.pendingUploadSummary).toBeNull();
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
    });

    it('resumes 100 paused files and also uploads the other 200 from a broader reselection', async () => {
        const files = filesFor(300);
        mocks.staged.mockRejectedValue(new Error('Test transfer failure'));
        const input = select(files.slice(0, 100));
        await advance(20000);
        expect(services.pendingUploadSummary?.fileCount).toBe(100);
        mocks.staged.mockResolvedValue(undefined);
        mocks.postUpload.mockClear();
        fireEvent.change(input, { target: { files } });
        for (let tick = 0; tick < 40 && finalizedFiles().length < 300; tick += 1) {
            await advance(1000);
        }
        await advance(1000);
        expect(finalizedFiles()).toHaveLength(300);
        expect(new Set(finalizedFiles().map((file) => file.filename)).size).toBe(300);
        expect(initFiles()).toHaveLength(200);
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.queuedUploadFileCount).toBe(0);
    });

    it('still skips a confirmed duplicate without transferring bytes', async () => {
        const file = filesFor(1)[0];
        const hash = Array.from(new TextEncoder().encode(file.name)).join('-');
        mocks.getUpload.mockResolvedValue({ hashes: { [hash]: 'already-stored.heic' } });
        select([file]);
        await advance(1000);
        expect(initFiles()).toHaveLength(0);
        expect(mocks.staged).not.toHaveBeenCalled();
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.notifications.some((notification) => notification.progress?.skippedDuplicateCount === 1)).toBe(true);
    });

    it('loads library hashes while the picker is open without downloading them twice', async () => {
        const file = filesFor(1)[0];
        const hash = Array.from(new TextEncoder().encode(file.name)).join('-');
        mocks.getUpload.mockResolvedValue({ hashes: { [hash]: 'already-stored.heic' } });
        const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
        fireEvent.click(view.getByText('Upload'));
        await advance(10000);
        fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [file] } });
        await advance(1000);
        expect(mocks.getUpload.mock.calls.filter(([url]) => url === '/upload/known-hashes')).toHaveLength(1);
        expect(mocks.staged).not.toHaveBeenCalled();
        expect(services.notifications.some((notification) => notification.progress?.skippedDuplicateCount === 1)).toBe(true);
    });

    it('replaces old downloaded library hashes when a fresh lookup returns no hashes', async () => {
        const file = filesFor(1)[0];
        const hash = Array.from(new TextEncoder().encode(file.name)).join('-');
        mocks.getUpload.mockResolvedValue({ hashes: { [hash]: 'deleted.heic' } });
        const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
        fireEvent.click(view.getByText('Upload'));
        await advance(1000);
        const input = view.container.querySelector('input[type="file"]')!;
        fireEvent(input, new Event('cancel'));
        mocks.getUpload.mockResolvedValue({ hashes: {} });
        fireEvent.change(input, { target: { files: [file] } });
        await advance(1000);
        expect(mocks.staged).toHaveBeenCalledTimes(1);
        expect(finalizedFiles()).toHaveLength(1);
    });

    it('ignores an older picker response arriving after a newer library lookup', async () => {
        const file = filesFor(1)[0];
        const hash = Array.from(new TextEncoder().encode(file.name)).join('-');
        let resolveOld!: (value: unknown) => void;
        mocks.getUpload.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
        const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
        fireEvent.click(view.getByText('Upload'));
        await advance(100);
        const input = view.container.querySelector('input[type="file"]')!;
        fireEvent(input, new Event('cancel'));
        mocks.getUpload.mockResolvedValue({ hashes: { unrelated: 'other.heic' } });
        fireEvent.click(view.getByText('Upload'));
        await advance(100);
        await act(async () => { resolveOld({ hashes: { [hash]: 'deleted.heic' } }); });
        fireEvent.change(input, { target: { files: [file] } });
        await advance(1000);
        expect(mocks.staged).toHaveBeenCalledTimes(1);
        expect(finalizedFiles()).toHaveLength(1);
    });

    it('does not reintroduce a deleted photo from a delayed library response', async () => {
        const file = filesFor(1)[0];
        const hash = Array.from(new TextEncoder().encode(file.name)).join('-');
        let resolveOld!: (value: unknown) => void;
        mocks.getUpload.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
        const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
        fireEvent.click(view.getByText('Upload'));
        await advance(100);
        act(() => { services.releaseKnownHashesForFilenames(['deleted.heic']); });
        mocks.getUpload.mockResolvedValue({ hashes: {} });
        await act(async () => { resolveOld({ hashes: { [hash]: 'deleted.heic' } }); });
        fireEvent.change(view.container.querySelector('input[type="file"]')!, { target: { files: [file] } });
        await advance(1000);
        expect(mocks.staged).toHaveBeenCalledTimes(1);
        expect(finalizedFiles()).toHaveLength(1);
    });

    it('skips a file already uploaded in this tab while the shared library lookup is still pending', async () => {
        mocks.getUpload.mockImplementation((url: string) => url === '/upload/known-hashes'
            ? new Promise(() => {}) : Promise.resolve({}));
        const files = filesFor(1);
        const input = select(files);
        await advance(3000);
        expect(finalizedFiles()).toHaveLength(1);
        expect(services.pendingUploadSummary).toBeNull();

        fireEvent.change(input, { target: { files } });
        await advance(1000);
        expect(initFiles()).toHaveLength(1);
        expect(finalizedFiles()).toHaveLength(1);
        expect(mocks.staged).toHaveBeenCalledTimes(1);
        expect(mocks.getUpload.mock.calls.filter(([url]) => url === '/upload/known-hashes')).toHaveLength(1);
        expect(mocks.span.mock.calls.filter(([name]) => name === 'upload.known_hashes.timeout')).toHaveLength(0);
        expect(services.notifications.some((notification) => notification.progress?.skippedDuplicateCount === 1)).toBe(true);
    });

    it('also reports a backend duplicate in the same skipped-duplicate counter after transferring bytes', async () => {
        mocks.getUpload.mockImplementation((url: string) => url === '/upload/known-hashes'
            ? new Promise(() => {}) : Promise.resolve({}));
        const postUpload = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url, payload) => {
            if (url === '/upload/finalize-batch') {
                return { results: payload.files.map((file: { filename: string }) => ({
                    filename: file.filename, complete: true, duplicates: [{ filename: 'already-stored.heic', type: 'exact' }],
                })) };
            }
            return postUpload(url, payload);
        });
        select(filesFor(1));
        await advance(4000);
        expect(initFiles()).toHaveLength(1);
        expect(finalizedFiles()).toHaveLength(1);
        expect(mocks.staged).toHaveBeenCalledTimes(1);
        expect(services.notifications.some((notification) => notification.progress?.skippedDuplicateCount === 1)).toBe(true);
    });

    it('records a native cancel without treating an ordinary cancellation as an error', async () => {
        const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
        fireEvent.click(view.getByText('Upload'));
        const input = view.container.querySelector('input[type="file"]')!;
        await advance(1000);
        fireEvent(input, new Event('cancel'));
        expect(mocks.span).toHaveBeenCalledWith('upload.picker.cancel', 1000, { rows: 0 });
        expect(initFiles()).toHaveLength(0);
        expect(mocks.toast).not.toHaveBeenCalled();
    });

    it('offers neutral recovery feedback after a long picker cancellation', async () => {
        const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
        fireEvent.click(view.getByText('Upload'));
        await advance(46000);
        fireEvent(view.container.querySelector('input[type="file"]')!, new Event('cancel'));
        expect(mocks.toast).toHaveBeenCalledWith(expect.stringContaining('No files were received'), { timeout: 8000 });
        expect(initFiles()).toHaveLength(0);
    });
});
