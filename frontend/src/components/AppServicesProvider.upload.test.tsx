import { __resetJustUploadedForTests, listJustUploaded } from '../services/justUploaded';
import React from 'react';
import { NotificationBell } from './AppServiceIndicators';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    thumbs: new WeakMap<object, Blob>(),
    get: vi.fn(),
    post: vi.fn(),
    getUpload: vi.fn(),
    postUpload: vi.fn(),
    postTools: vi.fn(),
    requestJobPoll: vi.fn(),
    transferred: vi.fn(),
    staged: vi.fn(),
    uploaded: vi.fn(),
    properties: vi.fn(),
    committed: vi.fn(),
    idbDelete: vi.fn(),
    idbGet: vi.fn(),
    toast: vi.fn(),
    span: vi.fn(),
    preparationEnabled: false,
    prepared: vi.fn(),
    pollAuthenticated: false,
    jobs: vi.fn(),
    processingMode: 'backend',
    runBrowserProcessing: vi.fn(),
    profile: vi.fn(),
    push: undefined as undefined | { onProcessing?: () => void; onConnection?: (connected: boolean) => void },
}));
vi.mock('../services/indexReadyPush', () => ({
    startIndexReadyPush: (_ready: () => void, options: typeof mocks.push) => { mocks.push = options; return () => { mocks.push = undefined; }; },
}));

vi.mock('../config/appConfig', () => ({ getRuntimeConfig: () => ({ processingMode: mocks.processingMode, browserUploadPreparation: mocks.preparationEnabled }) }));
vi.mock('../services/uploadPreparationPipeline', () => ({
    preparedThumbnail: (prepared: object) => mocks.thumbs.get(prepared),
    UploadPreparationPipeline: class {
        start = mocks.prepared;
        dispose() {}
    },
}));
vi.mock('../services/apiClient', () => ({
    get: mocks.get,
    post: mocks.post,
    getTools: vi.fn().mockResolvedValue({ready: true, building: false}),
    postTools: mocks.postTools,
    getUpload: mocks.getUpload,
    postUpload: mocks.postUpload,
    resolveApiUrl: (url: string) => url,
}));
vi.mock('../services/authClient', () => ({ isAuthEnabled: () => false, getAccessToken: async () => null }));
vi.mock('../services/jobNotifications', async (importOriginal) => ({
    ...await importOriginal<typeof import('../services/jobNotifications')>(),
    isLikelyAuthenticated: () => mocks.pollAuthenticated,
    fetchJobStatuses: mocks.jobs,
    requestJobPoll: mocks.requestJobPoll,
}));
vi.mock('../services/backgroundKeepAlive', () => ({
    BackgroundKeepAlive: class {
        start() {}
        stop() {}
        dispose() {}
    },
}));
vi.mock('../services/toast', () => ({ showToast: mocks.toast }));
vi.mock('./browserAiShared', async (importOriginal) => {
    const actual = await importOriginal<typeof import('./browserAiShared')>();
    mocks.profile.mockImplementation(actual.getAdaptiveUploadProfile);
    return { ...actual, getAdaptiveUploadProfile: mocks.profile };
});
vi.mock('../services/perf', () => ({ perf: { recordSpan: mocks.span, span: (_name: string, run: () => unknown) => run() } }));
vi.mock('./browserProcessingRuntime', () => ({
    idbPut: vi.fn().mockResolvedValue(undefined),
    idbGet: mocks.idbGet,
    idbDelete: mocks.idbDelete,
    readBlobArrayBuffer: async (file: File) => new TextEncoder().encode(file.name).buffer,
    sha256ArrayBuffer: async (buffer: ArrayBuffer) => Array.from(new Uint8Array(buffer)).join('-'),
    createBrowserThumbnail: vi.fn().mockResolvedValue(null),
    runBrowserProcessing: mocks.runBrowserProcessing,
}));
vi.mock('@azure/storage-blob', () => ({
    BlockBlobClient: class {
        stageBlock = (...args: unknown[]) => {
            mocks.staged(...args);
            return mocks.transferred(...args);
        };
        upload = (body: Blob, length: number, options: unknown) => {
            mocks.uploaded(body, length, options);
            return mocks.transferred('single-put', body, length, options);
        };
        getProperties = mocks.properties;
        commitBlockList = mocks.committed;
    },
}));

import { AppServicesProvider, useAppServices } from './AppServicesProvider';
import { UploadDock } from '../mockups/prototype/components/UploadDock';
import { MAX_UPLOAD_RETRY_ATTEMPTS, UPLOAD_SESSION_STORAGE_KEY } from './browserAiShared';
import * as largeFileHash from '../utils/largeFileHash';
import * as browserProcessing from './browserProcessingRuntime';
import * as uploadWaveSync from '../services/uploadWaveSync';

let services: ReturnType<typeof useAppServices>;
function Probe({ toolsPage = true }: { toolsPage?: boolean }) {
    services = useAppServices();
    const { registerToolsPageJobPolling } = services;
    React.useEffect(() => {
        if (toolsPage) return registerToolsPageJobPolling();
    }, [toolsPage, registerToolsPageJobPolling]);
    return <><button onClick={services.requestUpload}>Upload</button><UploadDock /></>;
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
    mocks.processingMode = 'backend';
    mocks.get.mockResolvedValue({});
    mocks.post.mockResolvedValue({});
    mocks.runBrowserProcessing.mockResolvedValue({ clientProcessing: {}, clientProcessingReport: [] });
    mocks.jobs.mockResolvedValue([]);
    mocks.postTools.mockResolvedValue({});
    mocks.prepared.mockResolvedValue({});
    vi.stubGlobal('localStorage', memoryStorage());
    vi.stubGlobal('sessionStorage', memoryStorage());
    localStorage.clear();
    sessionStorage.clear();
    mocks.getUpload.mockImplementation(async () => ({ hashes: {} }));
    mocks.transferred.mockResolvedValue(undefined);
    mocks.committed.mockResolvedValue(undefined);
    mocks.properties.mockRejectedValue(new Error('Blob not found'));
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
    vi.unstubAllEnvs();
});

function select(files: File[]) {
    const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
    fireEvent.click(view.getByText('Upload'));
    const input = view.container.querySelector('input[type="file"]')!;
    fireEvent.change(input, { target: { files } });
    return input;
}

describe('Tools-only job polling', () => {
    it('ignores mount, focus, visibility and mutation nudges outside Tools', async () => {
        mocks.pollAuthenticated = true;
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        const { requestJobPoll } = await vi.importActual<typeof import('../services/jobNotifications')>('../services/jobNotifications');
        await act(async () => {
            window.dispatchEvent(new Event('focus'));
            document.dispatchEvent(new Event('visibilitychange'));
            requestJobPoll();
        });
        await advance(60000);
        expect(mocks.jobs).not.toHaveBeenCalled();
    });

    it('starts on entering Tools and stops on leaving, including an in-flight response', async () => {
        mocks.pollAuthenticated = true;
        let resolveJobs!: (jobs: unknown[]) => void;
        mocks.jobs.mockImplementationOnce(() => new Promise(resolve => { resolveJobs = resolve; }));
        const view = render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        view.rerender(<AppServicesProvider><Probe /></AppServicesProvider>);
        expect(mocks.jobs).toHaveBeenCalledTimes(1);
        view.rerender(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await act(async () => resolveJobs([{jobId: 'running', kind: 'ipwork', status: 'running'}]));
        await advance(12000);
        expect(mocks.jobs).toHaveBeenCalledTimes(1);
        expect(services.activeJobs).toEqual([]);
        mocks.jobs.mockResolvedValue([{jobId: 'running', kind: 'ipwork', status: 'running'}]);
        view.rerender(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(6000);
        expect(mocks.jobs).toHaveBeenCalledTimes(3);
        view.rerender(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(12000);
        expect(mocks.jobs).toHaveBeenCalledTimes(3);
        expect(services.activeJobs).toEqual([]);
    });
});

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

describe('browser processing completion notifications', () => {
    const rerun = { actions: ['thumbnails', 'preview'] as const, items: [{ filename: 'gallery.jpg', rotation: 90 }], force: true };
    const startRerun = async () => {
        let processed = -1;
        await act(async () => {
            processed = await services.startBrowserProcessing({ ...rerun, actions: [...rerun.actions] });
        });
        return processed;
    };

    it('does not announce browser processing for backend-owned thumbnail and preview reruns', async () => {
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        expect(await startRerun()).toBe(0);
        await advance(16000);
        expect(services.notifications).toEqual([]);
        expect(mocks.postUpload).not.toHaveBeenCalled();
        expect(mocks.runBrowserProcessing).not.toHaveBeenCalled();
    });

    it('does not announce an AI-only run when the browser model is unavailable', async () => {
        mocks.processingMode = 'browser';
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        await act(async () => {
            await services.startBrowserProcessing({ filenames: ['gallery.jpg'], actions: ['faces'], force: true });
        });
        await advance(16000);
        expect(services.notifications).toEqual([]);
        expect(mocks.postUpload).not.toHaveBeenCalled();
    });

    it('reports a denied lease without claiming that zero photos finished processing', async () => {
        mocks.processingMode = 'browser';
        mocks.postUpload.mockResolvedValue({ claimed: false, reason: 'lease_held' });
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        expect(await startRerun()).toBe(0);
        await advance(16000);
        expect(services.notifications).toHaveLength(1);
        expect(services.notifications[0]).toMatchObject({ title: 'No photos processed', progress: undefined });
        expect(services.notifications[0].details).not.toContain('0/0');
        expect(mocks.runBrowserProcessing).not.toHaveBeenCalled();
    });

    it('reports a successful forced rerun and does not auto-pull unrelated photos for an items-only request', async () => {
        mocks.processingMode = 'browser';
        mocks.get.mockResolvedValue({ url: 'https://images.test/gallery.jpg' });
        mocks.postUpload.mockResolvedValue({ claimed: true, leaseId: 'browser-lease' });
        vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['image'], { type: 'image/jpeg' }) }));
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(0);
        // Ignore the provider's independent startup queue probe.
        mocks.getUpload.mockClear();
        expect(await startRerun()).toBe(1);
        await advance(16000);
        expect(services.notifications[0]).toMatchObject({
            title: 'Processing photos finished', details: 'Processed 1/1 photo.',
        });
        expect(mocks.runBrowserProcessing).toHaveBeenCalledTimes(1);
        expect(mocks.runBrowserProcessing.mock.calls[0][5]).toMatchObject({
            thumbnailRotationDegrees: 90, requestedSteps: new Set(['thumbnail', 'preview']),
        });
        expect(mocks.getUpload.mock.calls.some(([url]) => String(url).startsWith('/upload/processing/pending'))).toBe(false);
    });
});

describe('browser upload selection and startup', () => {
    const endSummaries = () => mocks.postUpload.mock.calls
        .filter(([url, payload]) => url === '/upload/telemetry' && payload.phase === 'end');

    it('finishes upload bookkeeping while the index enqueue and view refresh remain pending', async () => {
        mocks.postTools.mockImplementation(() => new Promise(() => {}));
        const input = select(filesFor(1));
        const refresh = vi.fn(() => new Promise<void>(() => {}));
        act(() => { services.registerUploadCompletionHandler(refresh); });
        await advance(4000);
        expect(mocks.postTools).toHaveBeenCalledWith('/api/tools/indexes/build', {
            scope: 'light', reason: 'upload-complete', wait: false,
        });
        expect(refresh).toHaveBeenCalledTimes(1);
        expect(services.uploading).toBe(false);
        expect(services.pendingUploadSummary).toBeNull();
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
        expect(endSummaries()).toHaveLength(1);
        expect(refreshRequests()).toHaveLength(1);
        expect(mocks.requestJobPoll).toHaveBeenCalled();
        fireEvent.change(input, {target: {files: [filesFor(2)[1]]}});
        await advance(4000);
        expect(finalizedFiles()).toHaveLength(2);
        expect(endSummaries()).toHaveLength(2);
    });

    it('starts the next queued upload without waiting for an index response', async () => {
        mocks.postTools.mockImplementation(() => new Promise(() => {}));
        let releaseTransfer!: () => void;
        mocks.transferred.mockImplementationOnce(() => new Promise<void>(resolve => { releaseTransfer = resolve; }));
        const files = filesFor(2);
        const input = select([files[0]]);
        await advance(1000);
        fireEvent.change(input, {target: {files: [files[1]]}});
        await advance(1000);
        expect(services.queuedUploadFileCount).toBe(1);
        await act(async () => { releaseTransfer(); });
        for (let tick = 0; tick < 8; tick += 1) await advance(1000);
        expect(finalizedFiles()).toHaveLength(2);
        expect(endSummaries()).toHaveLength(2);
        expect(services.queuedUploadFileCount).toBe(0);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('skips a repeated selection queued while its first upload is active', async () => {
        let releaseTransfer!: () => void;
        mocks.transferred.mockImplementationOnce(() => new Promise<void>(resolve => { releaseTransfer = resolve; }));
        const file = filesFor(1)[0];
        const input = select([file]);
        await advance(1000);

        fireEvent.change(input, { target: { files: [file] } });
        await advance(1000);
        expect(services.queuedUploadFileCount).toBe(1);

        await act(async () => { releaseTransfer(); });
        for (let tick = 0; tick < 12; tick += 1) await advance(1000);

        expect(initFiles()).toHaveLength(1);
        expect(finalizedFiles()).toHaveLength(1);
        expect(mocks.transferred).toHaveBeenCalledTimes(1);
        expect(services.queuedUploadFileCount).toBe(0);
        expect(services.notifications.some(notification =>
            notification.progress?.skippedDuplicateCount === 1)).toBe(true);
    });

    it('wakes job polling again after a delayed enqueue response', async () => {
        let resolveIndex!: (value: unknown) => void;
        mocks.postTools.mockImplementation(() => new Promise(resolve => { resolveIndex = resolve; }));
        select(filesFor(1));
        await advance(4000);
        expect(endSummaries()).toHaveLength(1);
        const nudges = mocks.requestJobPoll.mock.calls.length;
        await act(async () => { resolveIndex({queued: 'already_active', building: true}); });
        expect(mocks.requestJobPoll).toHaveBeenCalledTimes(nudges + 1);
    });

    it('refreshes views and reports index readiness after upload has already finished', async () => {
        mocks.pollAuthenticated = true;
        select(filesFor(1));
        const refresh = vi.fn();
        act(() => { services.registerDataRefreshHandler(refresh); });
        await advance(4000);
        expect(endSummaries()).toHaveLength(1);
        expect(refresh).not.toHaveBeenCalled();
        mocks.jobs.mockResolvedValue([{
            jobId: 'index-build-owner-light', kind: 'index_build', status: 'done', indexScope: 'light',
            title: 'Photo index updated', message: 'Photo ordering and access are ready.',
            updatedAt: new Date().toISOString(),
        }]);
        await act(async () => { window.dispatchEvent(new Event('focus')); });
        await advance(4000);
        expect(refresh).toHaveBeenCalledTimes(1);
        expect(services.notifications.some(n => n.title === 'Photo index updated')).toBe(true);
        expect(endSummaries()).toHaveLength(1);
    });

    it('keeps polling after a transient failure and refreshes immediately on index completion', async () => {
        mocks.pollAuthenticated = true;
        const queued = { jobId: 'index-build-owner-people', kind: 'index_build', status: 'queued',
            indexScope: 'people', title: 'People updating', message: '', updatedAt: new Date().toISOString() };
        mocks.jobs.mockResolvedValueOnce([queued]).mockRejectedValueOnce(new Error('temporary status failure'))
            .mockResolvedValueOnce([{ ...queued, status: 'done', updatedAt: new Date(Date.now() + 12000).toISOString() }])
            .mockResolvedValue([]);
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        const refresh = vi.fn();
        act(() => { services.registerDataRefreshHandler(refresh); });
        await advance(0);
        await advance(6000);
        expect(mocks.jobs).toHaveBeenCalledTimes(2);
        await advance(6001);
        expect(mocks.jobs).toHaveBeenCalledTimes(3);
        expect(refresh).toHaveBeenCalledTimes(1);
    });

    it('allows a queued job to become visible after a mutation nudge without focus or reload', async () => {
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        const refresh = vi.fn();
        act(() => { services.registerDataRefreshHandler(refresh); });
        mocks.pollAuthenticated = true;
        const { requestJobPoll } = await vi.importActual<typeof import('../services/jobNotifications')>('../services/jobNotifications');
        await act(async () => { requestJobPoll(); });
        expect(mocks.jobs).toHaveBeenCalledTimes(1);
        mocks.jobs.mockResolvedValue([{ jobId: 'index-build-owner-people', kind: 'index_build', status: 'done',
            indexScope: 'people', title: 'People updated', message: '', updatedAt: new Date().toISOString() }]);
        await advance(6001);
        expect(refresh).toHaveBeenCalledTimes(1);
    });

    it('bounds empty mutation polling instead of keeping idle sessions polling forever', async () => {
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        mocks.pollAuthenticated = true;
        const { requestJobPoll } = await vi.importActual<typeof import('../services/jobNotifications')>('../services/jobNotifications');
        await act(async () => { requestJobPoll(); });
        await advance(36000);
        const calls = mocks.jobs.mock.calls.length;
        expect(calls).toBeLessThanOrEqual(7);
        await advance(60000);
        expect(mocks.jobs).toHaveBeenCalledTimes(calls);
    });

    it('keeps successful uploads successful when indexing or refresh handlers fail', async () => {
        mocks.postTools.mockRejectedValue(new Error('Indexer unavailable'));
        select(filesFor(1));
        const healthy = vi.fn();
        act(() => {
            services.registerUploadCompletionHandler(() => { throw new Error('Refresh failed'); });
            services.registerUploadCompletionHandler(() => Promise.reject(new Error('Async refresh failed')));
            services.registerUploadCompletionHandler(healthy);
        });
        await advance(4000);
        expect(healthy).toHaveBeenCalledTimes(1);
        expect(endSummaries()).toHaveLength(1);
        expect(endSummaries()[0][1]).toMatchObject({uploaded: 1, failed: 0});
        expect(services.notifications.some(n => n.title === 'Upload failed')).toBe(false);
        // The finished entry reads as the redesign draws it, with the new photo for its thumbnail strip.
        expect(services.notifications.find(n => n.title === '1 item uploaded')?.photos).toHaveLength(1);
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.uploading).toBe(false);
    });

    it('prepares a photo in flight and finalizes it with its assets, also in backend mode', async () => {
        mocks.preparationEnabled = true;
        mocks.processingMode = 'backend';
        let resolvePreparation!: (value: unknown) => void;
        mocks.prepared.mockImplementation(() => new Promise(resolve => { resolvePreparation = resolve; }));
        select(filesFor(1));
        await advance(1000);
        // The original transfers without waiting for its preview/thumbnail.
        expect(mocks.transferred).toHaveBeenCalledTimes(1);
        expect(mocks.committed).not.toHaveBeenCalled();
        expect(mocks.uploaded).toHaveBeenCalledTimes(1);
        expect(mocks.prepared).toHaveBeenCalledTimes(1);
        expect(initFiles()[0]).toMatchObject({includePreview: true, includeMetadata: true});
        expect(finalizedFiles()).toHaveLength(0);
        const prepared = {clientProcessing: {preview: {alreadyUploaded: true}, thumbnail: {alreadyUploaded: true}},
            clientMetadata: {version: 1, bytes: 400, sha256: 'b'.repeat(64), sourceSha256: 'a'.repeat(64)},
            preparationReport: {version: 1, elapsedMs: 200, assets: {preview: {target: true, generation: 'generated', upload: 'uploaded'}}}};
        // The thumbnail the preparation drew is reused for the gallery tile, not decoded again.
        __resetJustUploadedForTests();
        mocks.thumbs.set(prepared, new Blob(['thumb']));
        vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => 'blob:prepared-thumb', revokeObjectURL: () => {} }));
        await act(async () => { resolvePreparation(prepared); });
        await advance(1000);
        expect(finalizedFiles()[0]).toMatchObject({...prepared, blobName: 'blob-original-0.heic'});
        expect(listJustUploaded().map(e => e.thumbnailUrl)).toEqual(['blob:prepared-thumb']);
        const batchId = finalizedFiles()[0].uploadBatchId;
        expect(batchId).toMatch(/^[0-9a-f-]{36}$/);
        await advance(3000);
        const summaries = mocks.postUpload.mock.calls.filter(([url]) => url === '/upload/telemetry');
        expect(summaries.map(([, payload]) => payload)).toEqual(expect.arrayContaining([
            expect.objectContaining({batchId, phase: 'start', selected: 1, photos: 1, videos: 0}),
            expect.objectContaining({batchId, phase: 'end', uploaded: 1, failed: 0, duplicates: 0}),
        ]));
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('coalesces files committed within the short finalize collection window', async () => {
        select(filesFor(3));
        await advance(150);
        const requests = mocks.postUpload.mock.calls.filter(([url]) => url === '/upload/finalize-batch');
        expect(requests).toHaveLength(1);
        expect(requests[0][1].files).toHaveLength(3);
        await advance(3000);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it.each([1, 1024 * 1024, 8 * 1024 * 1024])('uploads a fresh %i-byte original with one PUT and no block commit', async (size) => {
        const file = new File([new Uint8Array(size)], 'small.jpg', {type: 'image/jpeg'});
        select([file]);
        await advance(4000);
        expect(mocks.uploaded).toHaveBeenCalledExactlyOnceWith(file, size,
            expect.objectContaining({blobHTTPHeaders: {blobContentType: 'image/jpeg'}}));
        expect(mocks.staged).not.toHaveBeenCalled();
        expect(mocks.committed).not.toHaveBeenCalled();
        expect(finalizedFiles()).toHaveLength(1);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('keeps the block upload path above the single-PUT limit', async () => {
        select([new File([new Uint8Array(8 * 1024 * 1024 + 1)], 'larger.jpg', {type: 'image/jpeg'})]);
        await advance(4000);
        expect(mocks.uploaded).not.toHaveBeenCalled();
        expect(mocks.staged).toHaveBeenCalled();
        expect(mocks.committed).toHaveBeenCalledTimes(1);
        expect(finalizedFiles()).toHaveLength(1);
    });

    it('retries a transient single-PUT failure without staging blocks', async () => {
        mocks.transferred.mockRejectedValueOnce(Object.assign(new Error('network error'), {statusCode: 503}));
        select(filesFor(1));
        await advance(4000);
        expect(mocks.uploaded).toHaveBeenCalledTimes(2);
        expect(mocks.staged).not.toHaveBeenCalled();
        expect(mocks.committed).not.toHaveBeenCalled();
        expect(finalizedFiles()).toHaveLength(1);
    });

    it.each([2, 0])('verifies an aborted single PUT against committed size %i before finalizing', async (size) => {
        const error = Object.assign(new Error('Aborted'), {name: 'AbortError'});
        if (size === 2) mocks.transferred.mockRejectedValueOnce(error);
        else mocks.transferred.mockRejectedValue(error);
        mocks.properties.mockResolvedValue({contentLength: size});
        select(filesFor(1));
        await advance(4000);
        expect(mocks.uploaded).toHaveBeenCalledTimes(1);
        expect(mocks.properties).toHaveBeenCalledTimes(1);
        expect(finalizedFiles()).toHaveLength(size === 2 ? 1 : 0);
    });

    it('keeps transfer heartbeats active for a slow single PUT and stops them on Stop', async () => {
        mocks.transferred.mockImplementation((_id, _body, _length, options) => new Promise((_resolve, reject) => {
            options.abortSignal.addEventListener('abort', () => reject(new Error('upload_stopped_by_user')), {once: true});
        }));
        select(filesFor(1));
        await advance(91_000);
        const heartbeats = () => mocks.postUpload.mock.calls.filter(([url, body]) => url === '/upload/transfer-heartbeat'
            && Object.keys(body).length === 0);
        expect(heartbeats()).toHaveLength(1);
        act(() => services.stopActiveUpload());
        await advance(95_000);
        expect(heartbeats()).toHaveLength(1);
        expect(finalizedFiles()).toHaveLength(0);
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
        await advance(4000);
        expect(initFiles().map((file) => file.filename).sort()).toEqual(files.map((file) => file.name).sort());
        expect(finalizedFiles()).toHaveLength(count);
        expect(new Set(finalizedFiles().map((file) => file.filename)).size).toBe(count);
        expect(finalizedFiles().every((file) => file.totalSize === 2 && file.contentType === 'image/heic')).toBe(true);
        expect(mocks.transferred).toHaveBeenCalledTimes(count);
        expect(refreshRequests()).toHaveLength(1);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'completed' });
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.queuedUploadFileCount).toBe(0);
        expect(writes.mock.calls.filter(([key]) => key === UPLOAD_SESSION_STORAGE_KEY).length).toBeLessThan(count / 10);
        expect(mocks.span).toHaveBeenCalledWith('upload.picker.change', expect.any(Number), {
            rows: count, bytes: count * 2,
        });
    });

    it('starts transfers after exhausting known-hash retries, even if the app never responds', async () => {
        // Upload click now blocks on waking the app / fetching known hashes
        // (up to UPLOAD_HASH_DOWNLOAD_ATTEMPTS=5 retries) before anything
        // else starts -- this simulates the app never answering at all, and
        // confirms the upload still proceeds once retries are exhausted
        // rather than hanging forever.
        mocks.getUpload.mockImplementation((url: string) => url === '/upload/known-hashes'
            ? new Promise(() => {}) : Promise.resolve({}));
        select(filesFor(3));
        await advance(330000);
        expect(initFiles()).toHaveLength(3);
        expect(finalizedFiles()).toHaveLength(3);
        expect(mocks.transferred).toHaveBeenCalledTimes(3);
    });

    it('blocks upload start until the known-hash download settles, then skips every already-known duplicate', async () => {
        const files = filesFor(50);
        let resolveHashes!: (value: unknown) => void;
        mocks.getUpload.mockImplementation((url: string) => url === '/upload/known-hashes'
            ? new Promise((resolve) => { resolveHashes = resolve; }) : Promise.resolve({}));
        select(files);
        await advance(1000);
        // Nothing starts while the blocking wake-up/hash call is still pending.
        expect(mocks.transferred).not.toHaveBeenCalled();
        await act(async () => {
            resolveHashes({ hashes: Object.fromEntries(files.slice(0, 20).map((file) => [
                Array.from(new TextEncoder().encode(file.name)).join('-'), `stored-${file.name}`,
            ])) });
        });
        await advance(10000);
        expect(mocks.transferred.mock.calls.length).toBeGreaterThan(0);
        expect(initFiles()).toHaveLength(30);
        expect(services.notifications.some((notification) =>
            notification.progress?.skippedDuplicateCount === 20)).toBe(true);
        expect(mocks.getUpload.mock.calls.filter(([url]) => url === '/upload/known-hashes')).toHaveLength(1);
    });

    it('asks the server about photos the snapshot misses and never uploads the ones it already holds', async () => {
        const files = filesFor(30);
        const hashOf = (file: File) => Array.from(new TextEncoder().encode(file.name)).join('-');
        const base = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any, ...rest: unknown[]) => url === '/upload/known-hashes/check'
            ? { known: Object.fromEntries(files.slice(0, 12).map((file) => [hashOf(file), `stored-${file.name}`])) }
            : base(url, payload, ...rest));
        select(files);
        await advance(10000);
        expect(initFiles()).toHaveLength(18);
        expect(mocks.postUpload.mock.calls.filter(([url]) => url === '/upload/known-hashes/check')).toHaveLength(1);
        expect(services.notifications.some((notification) =>
            notification.progress?.skippedDuplicateCount === 12)).toBe(true);
    });

    it('uploads normally when the server hash check fails', async () => {
        const base = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any, ...rest: unknown[]) => {
            if (url === '/upload/known-hashes/check') throw new Error('down');
            return base(url, payload, ...rest);
        });
        select(filesFor(5));
        await advance(10000);
        expect(initFiles()).toHaveLength(5);
    });

    it('refreshes after a run ends with failed files', async () => {
        mocks.transferred.mockRejectedValue(new Error('Test transfer failure'));
        select(filesFor(2));
        await advance(20000);
        expect(services.pendingUploadSummary?.fileCount).toBe(2);
        expect(refreshRequests()).toHaveLength(1);
        expect(refreshRequests()[0][1]).toEqual({ reason: 'failed' });
    });

    it('refreshes after stopping an active upload', async () => {
        mocks.transferred.mockImplementation((_id, _data, _size, options) => new Promise((_resolve, reject) => {
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

    it('pausing lets the files already uploading finish, starts no more, and keeps the rest for Resume', async () => {
        const releases: Array<() => void> = [];
        let aborted = false;
        mocks.transferred.mockImplementation((_id, _data, _size, options) => new Promise<void>((resolve, reject) => {
            options.abortSignal.addEventListener('abort', () => { aborted = true; reject(new Error('upload_stopped_by_user')); }, { once: true });
            releases.push(resolve);
        }));
        select(filesFor(50));
        await advance(1000);
        const inFlight = releases.length;
        expect(inFlight).toBeGreaterThan(0);
        expect(inFlight).toBeLessThan(50);
        act(() => { services.pauseActiveUpload(); });
        await advance(500);
        expect(aborted).toBe(false);
        expect(services.uploadActive).toBe(true);
        releases.splice(0).forEach((release) => release());
        await advance(3000);
        expect(mocks.transferred).toHaveBeenCalledTimes(inFlight);
        expect(services.uploadActive).toBe(false);
        expect(services.pendingUploadSummary?.fileCount).toBe(50 - inFlight);
        expect(services.pendingUploadSummary?.failedCount).toBe(0);
        expect(aborted).toBe(false);
    });

    it('keeps one calm notification entry through pause and resume', async () => {
        const releases: Array<() => void> = [];
        mocks.transferred.mockImplementation(() => new Promise<void>((resolve) => { releases.push(resolve); }));
        select(filesFor(50));
        await advance(1000);
        act(() => { services.pauseActiveUpload(); });
        releases.splice(0).forEach((release) => release());
        await advance(3000);
        const stories = () => (services.notifications as Array<{ story?: { sessionId: string; state: string; pausedAt?: number; resumedAt?: number }; title: string }>).filter((n) => n.story);
        expect(stories()).toHaveLength(1);
        expect(stories()[0].story?.state).toBe('paused');
        expect(stories()[0].title).toBe('Upload paused');
        expect(stories()[0].story?.pausedAt).toBeGreaterThan(0);
        mocks.transferred.mockImplementation(() => Promise.resolve());
        // The bell's Resume names the entry's own upload id.
        act(() => { void services.retryPersistedUploadSession(stories()[0].story?.sessionId, 'resume'); });
        await advance(5000);
        expect(stories()).toHaveLength(1);
        expect(stories()[0].story?.resumedAt).toBeGreaterThan(0);
    });

    it('resume leaves failed files for the end and Try again retries only them', async () => {
        const releases: Array<() => void> = [];
        mocks.transferred.mockRejectedValueOnce(Object.assign(new Error('bad'), { statusCode: 400 }));
        mocks.transferred.mockImplementation(() => new Promise<void>((resolve) => { releases.push(resolve); }));
        select(filesFor(50));
        await advance(1000);
        act(() => { services.pauseActiveUpload(); });
        releases.splice(0).forEach((release) => release());
        await advance(3000);
        const summary = services.pendingUploadSummary;
        expect(summary?.failedCount).toBe(1);
        expect(summary!.fileCount).toBeGreaterThan(1);
        const waiting = summary!.fileCount - 1;
        const callsBefore = mocks.transferred.mock.calls.length;
        mocks.transferred.mockImplementation(() => Promise.resolve());
        act(() => { void services.retryPersistedUploadSession(undefined, 'resume'); });
        await advance(8000);
        expect(mocks.transferred.mock.calls.length - callsBefore).toBe(waiting);
        expect(services.pendingUploadSummary).toEqual({ fileCount: 1, failedCount: 1 });
        const callsAfterResume = mocks.transferred.mock.calls.length;
        act(() => { void services.retryPersistedUploadSession(undefined, 'failed'); });
        await advance(8000);
        expect(mocks.transferred.mock.calls.length - callsAfterResume).toBe(1);
    });

    it('cancelling after a pause drops the files that never started but keeps the failed ones', async () => {
        mocks.transferred.mockRejectedValueOnce(Object.assign(new Error('bad'), { statusCode: 400 }));
        select(filesFor(40));
        await advance(500);
        act(() => { services.pauseActiveUpload(); });
        await advance(3000);
        const before = services.pendingUploadSummary;
        if (before && before.failedCount > 0) {
            await act(async () => { await services.cancelRemainingUpload(); });
            await advance(500);
            expect(services.pendingUploadSummary?.fileCount).toBe(before.failedCount);
            expect(services.pendingUploadSummary?.failedCount).toBe(before.failedCount);
        } else {
            await act(async () => { await services.cancelRemainingUpload(); });
            await advance(500);
            expect(services.pendingUploadSummary).toBeNull();
        }
    });

    it('shows a checking state in the dock while the selection is hashed for duplicates', async () => {
        let releaseKnownHashes!: () => void;
        mocks.getUpload.mockImplementation((url: string) => url === '/upload/known-hashes'
            ? new Promise((resolve) => { releaseKnownHashes = () => resolve({ hashes: {} }); })
            : Promise.resolve({ hashes: {} }));
        select(filesFor(30));
        await advance(1000);
        expect(services.uploadCheckProgress).toEqual({ checked: 0, total: 30 });
        expect(screen.getByText('Checking 30 photos')).toBeTruthy();
        expect(screen.getByText('0 of 30 checked')).toBeTruthy();
        await act(async () => { releaseKnownHashes(); });
        await advance(3000);
        expect(services.uploadCheckProgress).toBeNull();
        expect(screen.queryByText('Checking 30 photos')).toBeNull();
    });

    it('keeps upload progress and Stop available after notifications are cleared', async () => {
        mocks.transferred.mockImplementation((_id, _data, _size, options) => new Promise((_resolve, reject) => {
            const abort = () => reject(new Error('upload_stopped_by_user'));
            if (options.abortSignal.aborted) abort();
            else options.abortSignal.addEventListener('abort', abort, { once: true });
        }));
        select(filesFor(50));
        await advance(1000);
        expect(services.uploadActive).toBe(true);
        expect(screen.getByText('Uploading 50 items')).toBeTruthy();
        act(() => services.clearNotifications());
        expect(services.notifications).toEqual([]);
        expect(screen.getByText('Uploading 50 items')).toBeTruthy();
        // Pause now waits for files in flight; a hard stop is still what aborts them.
        act(() => services.stopActiveUpload());
        await advance(3000);
        expect(services.uploadActive).toBe(false);
        expect(services.uploadProgress).toBeNull();
        expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
        expect(refreshRequests()[0][1]).toEqual({ reason: 'stopped' });
    });

    it('can stop an automatic retry after notification dismissal with no finalization pending', async () => {
        mocks.transferred.mockRejectedValue(new Error('Test transfer failure'));
        select(filesFor(1));
        for (let tick = 0; tick < 200 && !services.notifications.some(n => n.title === 'Retrying failed files'); tick += 1) {
            await advance(100);
        }
        expect(services.notifications.some(n => n.title === 'Retrying failed files')).toBe(true);
        expect(services.uploading).toBe(false);
        expect(services.uploadActive).toBe(true);
        expect(finalizedFiles()).toHaveLength(0);
        const attempts = mocks.transferred.mock.calls.length;
        act(() => services.clearNotifications());
        fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
        await advance(10000);
        expect(mocks.transferred).toHaveBeenCalledTimes(attempts);
        expect(services.uploadActive).toBe(false);
        expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
    });

    it.each([false,true])('stops finalization retries promptly while offline=%s', async (offline) => {
        const original=mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url,payload) => {
            if (url==='/upload/finalize-batch') {
                if (offline) vi.spyOn(navigator,'onLine','get').mockReturnValue(false);
                throw {response:{status:503,data:{code:'wave_admission_retry'}}};
            }
            return original(url,payload);
        });
        select(filesFor(1));
        await advance(2000);
        expect(finalizedFiles().length).toBeGreaterThanOrEqual(1);
        expect(services.uploadActive).toBe(true);
        act(() => services.stopActiveUpload());
        await advance(100);
        expect(services.uploadActive).toBe(false);
        expect(services.pendingUploadSummary).toBeNull();
        const attempts=finalizedFiles().length;
        await advance(90000);
        expect(finalizedFiles()).toHaveLength(attempts);
        expect(refreshRequests().some(([,body]) => body.reason==='stopped')).toBe(true);
    });

    it('keeps the recovery checkpoint when Stop cannot acknowledge server closure', async () => {
        vi.spyOn(uploadWaveSync,'stopWaveShadowSession').mockRejectedValue(new Error('Server closure unavailable'));
        mocks.transferred.mockImplementation((_id,_data,_size,options) => new Promise((_resolve,reject) => {
            const abort=() => reject(new Error('upload_stopped_by_user'));
            if (options.abortSignal.aborted) abort();
            else options.abortSignal.addEventListener('abort',abort,{once:true});
        }));
        select(filesFor(2));
        await advance(1000);
        act(() => services.stopActiveUpload());
        await advance(3000);
        expect(services.uploadActive).toBe(false);
        expect(services.pendingUploadSummary?.fileCount).toBe(2);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).not.toBeNull();
        expect(services.notifications.some(n => n.title==='Upload stopped; closure pending')).toBe(true);
    });

    it('keeps Stop available during finalization and clears queued uploads after notification dismissal', async () => {
        let releaseFinalize!: () => void;
        const gate = new Promise<void>(resolve => { releaseFinalize = resolve; });
        const originalPost = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url, payload) => {
            if (url === '/upload/finalize-batch') await gate;
            return originalPost(url, payload);
        });
        select(filesFor(3));
        await advance(2000);
        expect(finalizedFiles()).toHaveLength(3);
        expect(services.uploading).toBe(false);
        expect(services.uploadActive).toBe(true);
        await act(async () => { await services.startUpload([new File(['queued'], 'queued.heic')]); });
        expect(services.queuedUploadFileCount).toBe(1);
        act(() => services.clearNotifications());
        expect(services.notifications).toEqual([]);
        expect(screen.getByText(/Finishing upload/)).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
        expect(services.queuedUploadFileCount).toBe(0);
        await act(async () => releaseFinalize());
        await advance(3000);
        expect(services.uploadActive).toBe(false);
        expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull();
        expect(initFiles().some(file => file.filename === 'queued.heic')).toBe(false);
    });

    it('includes late finalization when stopping the remaining transfers', async () => {
        let releaseFinalize!: () => void;
        const finalizeGate = new Promise<void>((resolve) => { releaseFinalize = resolve; });
        const originalPost = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url, payload) => {
            if (url === '/upload/finalize-batch') await finalizeGate;
            return originalPost(url, payload);
        });
        mocks.transferred.mockResolvedValueOnce(undefined).mockImplementation((_id, _data, _size, options) =>
            new Promise((_resolve, reject) => {
                const abort = () => reject(new Error('upload_stopped_by_user'));
                if (options.abortSignal.aborted) abort();
                else options.abortSignal.addEventListener('abort', abort, { once: true });
            }));
        select(filesFor(50));
        await advance(2000);
        expect(finalizedFiles()).toHaveLength(1);
        act(() => { services.stopActiveUpload(); });
        await advance(3000);
        expect(services.uploading).toBe(false);
        expect(refreshRequests()).toHaveLength(0);
        await act(async () => { releaseFinalize(); });
        await advance(1000);
        for (let tick=0; tick<10 && services.uploadActive; tick+=1) await advance(100);
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
        await advance(2000);
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
        mocks.transferred.mockImplementation((_id, _data, _size, options) => new Promise((_resolve, reject) => {
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

    it('finishes duplicate-only selections without creating upload checkpoints', async () => {
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
        expect(mocks.transferred).not.toHaveBeenCalled();
        expect(refreshRequests()[0][1]).toEqual({ reason: 'completed' });
        expect(skippedKeys.size).toBe(0);
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
        expect(mocks.transferred).not.toHaveBeenCalled();
        expect(mocks.committed).toHaveBeenCalledWith([blockId], expect.anything());
        expect(mocks.uploaded).not.toHaveBeenCalled();
        expect(finalizedFiles()).toHaveLength(1);
        expect(mocks.postUpload.mock.calls.find(([url]) => url === '/upload/init')![1]).toMatchObject({ uploadId: file.name });
        expect(services.pendingUploadSummary).toBeNull();
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
    });

    it('settles a resumed file the server already stores without staging or finalizing it again', async () => {
        // A finalize that stored the photo but never reached the browser.
        // Resuming must not reserve a new blob on the finished photo's row.
        const file = filesFor(1)[0];
        mocks.idbGet.mockResolvedValue(file);
        localStorage.setItem(UPLOAD_SESSION_STORAGE_KEY, JSON.stringify({
            id: 'stored', createdAt: Date.now(), files: [{
                key: 'stored:0', name: file.name, size: file.size, type: file.type,
                lastModified: file.lastModified, uploadedBytes: file.size, skippedDuplicate: false,
                status: 'finalizing', uploadId: file.name, blobName: `blob-${file.name}`,
                blockIds: [btoa('block-00000000')], chunkSizeBytes: file.size,
            }],
        }));
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => url === '/upload/init'
            ? { alreadyStored: true, uploadId: payload.uploadId, filename: 'original-0-0123abcd.heic',
                duplicates: [{ filename: 'original-0-0123abcd.heic', type: 'exact' }] }
            : original(url, payload));
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        await advance(4000);
        expect(mocks.postUpload.mock.calls.filter(([url]) => url === '/upload/init')).toHaveLength(1);
        expect(mocks.committed).not.toHaveBeenCalled();
        expect(finalizedFiles()).toHaveLength(0);
        expect(services.pendingUploadSummary).toBeNull();
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
    });

    it('resumes 100 paused files and also uploads the other 200 from a broader reselection', async () => {
        const files = filesFor(300);
        mocks.transferred.mockRejectedValue(new Error('Test transfer failure'));
        const input = select(files.slice(0, 100));
        await advance(20000);
        expect(services.pendingUploadSummary?.fileCount).toBe(100);
        mocks.transferred.mockResolvedValue(undefined);
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

    it('reselecting an already-uploaded-and-removed file dedups by hash instead of a stale local match', async () => {
        const files = filesFor(5);
        const failingNames = new Set(['original-3.heic', 'original-4.heic']);
        const failingNamesAtStart = new Set(failingNames);
        mocks.postUpload.mockImplementation(async (url: string, payload: { filename: string; files: Array<{ filename: string }> }) => {
            if (url === '/upload/init') {
                return { uploadId: payload.filename, blobName: `blob-${payload.filename}`,
                    blobUrl: `https://test.blob.core.windows.net/images/${payload.filename}` };
            }
            if (url === '/upload/init-batch') {
                return { results: payload.files.map((file) => ({
                    uploadId: file.filename, blobName: `blob-${file.filename}`,
                    blobUrl: `https://test.blob.core.windows.net/images/${file.filename}`,
                    uploadPreparationEnabled: true,
                    previewBlobUrl: 'https://test.blob.core.windows.net/thumbnails/preview.jpg',
                    metadataBlobUrl: 'https://test.blob.core.windows.net/thumbnails/metadata.bin',
                })) };
            }
            if (url === '/upload/finalize-batch') {
                return { results: payload.files.map((file) => (
                    failingNames.has(file.filename)
                        ? { filename: file.filename, error: 'Test finalize failure' }
                        : { filename: file.filename, complete: true, duplicates: [] }
                )) };
            }
            return {};
        });
        const input = select(files);
        await advance(20000);
        // Confirms the core fix directly: fileCount reflects only the 2 still-
        // failed files, not all 5 -- the 3 succeeded files are already gone
        // from the checkpoint (removePersistedFile), not merely marked done.
        expect(services.pendingUploadSummary?.fileCount).toBe(2);
        expect(finalizedFiles().filter((file) => !failingNames.has(file.filename))).toHaveLength(3);

        // The 3 already-uploaded files are now known server-side duplicates
        // (as they genuinely would be, having actually uploaded). Stop
        // failing finalize from here on (in place -- failingNames.clear()
        // rather than swapping the mock implementation, so every other
        // endpoint this flow needs keeps the same coverage it already had).
        mocks.getUpload.mockResolvedValue({ hashes: Object.fromEntries(
            files.filter((file) => !failingNames.has(file.name))
                .map((file) => [Array.from(new TextEncoder().encode(file.name)).join('-'), `stored-${file.name}`]),
        ) });
        failingNames.clear();
        mocks.transferred.mockClear();
        mocks.postUpload.mockClear();
        // fetchKnownHashes caches for 60s -- advance past it so the reselect
        // below queries the updated hashes above instead of the stale (empty)
        // ones fetched during the first run.
        await advance(61000);
        // Reselecting the FULL original 5-file picker selection -- same as a
        // user re-adding the same folder -- not just the 2 that paused.
        fireEvent.change(input, { target: { files } });
        for (let tick = 0; tick < 40 && finalizedFiles().length < 2; tick += 1) {
            await advance(1000);
        }
        // The 3 deduped files are queued (not started immediately, since the
        // 2 reattached retries were already running) -- give their own
        // dedup-only run time to complete and emit its notification too.
        await advance(20000);
        expect(services.pendingUploadSummary).toBeNull();
        // Only the 2 originally-failed files go through finalize this round.
        expect(finalizedFiles()).toHaveLength(2);
        expect(new Set(finalizedFiles().map((file) => file.filename))).toEqual(failingNamesAtStart);
        // The 3 already-done files are caught by the hash-dedup check before
        // any bytes transfer -- not silently skipped via a stale local match,
        // and not re-uploaded/duplicated either.
        expect(mocks.transferred).not.toHaveBeenCalled();
        expect(services.notifications.some((notification) => notification.progress?.skippedDuplicateCount === 3)).toBe(true);
    });

    it('collapses identical files before declaring or transferring the upload selection', async () => {
        vi.spyOn(largeFileHash, 'hashLargeFile').mockResolvedValue('a'.repeat(64));
        const files = filesFor(2);
        files.forEach(file => Object.defineProperty(file, 'size', { value: 256 * 1024 * 1024 }));
        const postUpload = mocks.postUpload.getMockImplementation()!;
        const pending: Array<() => void> = [];
        mocks.postUpload.mockImplementation((url, payload) => {
            if (url === '/upload/finalize-batch') {
                return new Promise(resolve => pending.push(() => resolve({
                    results: payload.files.map((file: { filename: string }) => ({
                        filename: file.filename, complete: true, duplicates: [],
                    })),
                })));
            }
            return postUpload(url, payload);
        });
        select(files);
        await advance(4000);
        expect(initFiles()).toHaveLength(1);
        expect(finalizedFiles()).toHaveLength(1);
        expect(services.notifications.some(notification =>
            notification.progress?.skippedDuplicateCount === 1)).toBe(true);
        await act(async () => { pending.forEach(resolve => resolve()); });
        await advance(4000);
        expect(initFiles()).toHaveLength(1);
        expect(finalizedFiles()).toHaveLength(1);
        expect(services.notifications.some(notification =>
            notification.progress?.skippedDuplicateCount === 1)).toBe(true);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('caches the finalized filename for later copies after the backend renames a source', async () => {
        const postUpload = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation((url, payload) => url === '/upload/finalize-batch'
            ? Promise.resolve({ results: payload.files.map(() => ({ filename: 'renamed.heic', complete: true, duplicates: [] })) })
            : postUpload(url, payload));
        const file = filesFor(1)[0];
        const input = select([file]);
        await advance(4000);
        expect(finalizedFiles()).toHaveLength(1);
        fireEvent.change(input, { target: { files: [file] } });
        await advance(4000);
        expect(initFiles()).toHaveLength(1);
        expect(services.notifications.some(notification =>
            notification.progress?.skippedDuplicateCount === 1)).toBe(true);
    });

    it('still skips a confirmed duplicate without transferring bytes', async () => {
        const file = filesFor(1)[0];
        const hash = Array.from(new TextEncoder().encode(file.name)).join('-');
        mocks.getUpload.mockResolvedValue({ hashes: { [hash]: 'already-stored.heic' } });
        select([file]);
        await advance(1000);
        expect(initFiles()).toHaveLength(0);
        expect(mocks.transferred).not.toHaveBeenCalled();
        expect(mocks.postUpload.mock.calls.some(([url]) => String(url).startsWith('/upload/waves/'))).toBe(false);
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
        expect(mocks.transferred).not.toHaveBeenCalled();
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
        await advance(2000);
        expect(mocks.transferred).toHaveBeenCalledTimes(1);
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
        await advance(2000);
        expect(mocks.transferred).toHaveBeenCalledTimes(1);
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
        await advance(2000);
        expect(mocks.transferred).toHaveBeenCalledTimes(1);
        expect(finalizedFiles()).toHaveLength(1);
    });

    it('skips a file already uploaded in this tab on a later selection', async () => {
        const files = filesFor(1);
        const input = select(files);
        await advance(3000);
        expect(finalizedFiles()).toHaveLength(1);
        expect(services.pendingUploadSummary).toBeNull();

        fireEvent.change(input, { target: { files } });
        await advance(1000);
        expect(initFiles()).toHaveLength(1);
        expect(finalizedFiles()).toHaveLength(1);
        expect(mocks.transferred).toHaveBeenCalledTimes(1);
        expect(mocks.span.mock.calls.filter(([name]) => name === 'upload.known_hashes.timeout')).toHaveLength(0);
        expect(services.notifications.some((notification) => notification.progress?.skippedDuplicateCount === 1)).toBe(true);
    });

    it('also reports a backend duplicate in the same skipped-duplicate counter after transferring bytes', async () => {
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
        expect(mocks.transferred).toHaveBeenCalledTimes(1);
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

describe('large upload hashing', () => {
    it('uses the bounded hasher before uploading a large file', async () => {
        const file = filesFor(1)[0];
        Object.defineProperty(file, 'size', { value: 2381066480 });
        const hash = vi.spyOn(largeFileHash, 'hashLargeFile').mockResolvedValue('a'.repeat(64));
        select([file]);
        await advance(4000);
        expect(hash).toHaveBeenCalledWith(file, expect.any(AbortSignal));
        expect(finalizedFiles()).toHaveLength(1);
        expect(finalizedFiles()[0]).toMatchObject({ sha256: 'a'.repeat(64), totalSize: 2381066480 });
    });

    it('aborts large-file hashing on Stop without admitting or finalizing bytes', async () => {
        const file = filesFor(1)[0];
        Object.defineProperty(file, 'size', { value: 2381066480 });
        const hash = vi.spyOn(largeFileHash, 'hashLargeFile').mockImplementation((_file, signal) =>
            new Promise((_resolve, reject) => signal!.addEventListener('abort', () =>
                reject(new DOMException('Stopped', 'AbortError')), { once: true })));
        select([file]);
        await advance(1000);
        expect(hash).toHaveBeenCalledTimes(1);
        act(() => services.stopActiveUpload());
        await advance(3000);
        expect(initFiles()).toHaveLength(0);
        expect(finalizedFiles()).toHaveLength(0);
        expect(services.uploading).toBe(false);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('stops the duplicate check on Stop even when every file is small', async () => {
        const reads = vi.spyOn(browserProcessing, 'readBlobArrayBuffer').mockImplementation(async (file: Blob) => {
            await new Promise(resolve => setTimeout(resolve, 100));
            return new TextEncoder().encode((file as File).name).buffer as ArrayBuffer;
        });
        select(filesFor(40));
        await advance(250);
        expect(reads.mock.calls.length).toBeGreaterThan(0);
        act(() => services.stopActiveUpload());
        const readsAtStop = reads.mock.calls.length;
        await advance(3000);
        expect(reads.mock.calls.length).toBeLessThanOrEqual(readsAtStop + 4);
        expect(reads.mock.calls.length).toBeLessThan(40);
        expect(initFiles()).toHaveLength(0);
        expect(services.uploading).toBe(false);
        expect(services.uploadCheckProgress).toBeNull();
    });
});

// Connects the real upload path to the backend's opt-in shadow wave protocol
// (backend/routes/upload_wave_shadow.py) -- see uploadWaveClient.ts/
// uploadWaveSync.ts. These calls are entirely best-effort: they must never
// block, delay, or fail the real upload, which the last test in this block
// exercises directly.
describe('unsupported files at selection', () => {
    const withSidecars = () => [
        ...filesFor(2),
        new File(['x'], 'IMG_O9346.AAE', { type: '' }),
        new File(['y'], 'IMG_O9347.aae', { type: '' }),
    ];

    it('drops sidecars handed over by drag-and-drop before anything registers', async () => {
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
        act(() => { void services.startUpload(withSidecars()); });
        await advance(4000);
        expect(initFiles().map((file: { filename: string }) => file.filename).sort()).toEqual(['original-0.heic', 'original-1.heic']);
        expect(services.notifications.find(n => n.title === 'Some files skipped')?.details).toContain('Skipped 2 files');
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('offers only photo and video types in the File System Access picker and drops the rest', async () => {
        const picked = withSidecars();
        const picker = vi.fn().mockResolvedValue(picked.map(file => ({
            kind: 'file', name: file.name, getFile: async () => file,
            queryPermission: async () => 'granted', requestPermission: async () => 'granted',
        })));
        vi.stubGlobal('showOpenFilePicker', picker);
        const view = render(<AppServicesProvider><Probe /></AppServicesProvider>);
        fireEvent.click(view.getByText('Upload'));
        await advance(4000);
        const accept = picker.mock.calls[0][0].types[0].accept['image/*'];
        expect(accept).toEqual(expect.arrayContaining(['.heic', '.cr3', '.mov']));
        expect(accept).not.toContain('.aae');
        expect(initFiles().map((file: { filename: string }) => file.filename).sort()).toEqual(['original-0.heic', 'original-1.heic']);
    });
});

describe('upload wave shadow protocol wiring', () => {
    let waveUUIDSequence = 0;
    const shadowCalls = (suffix: string) => mocks.postUpload.mock.calls
        .filter(([url]) => typeof url === 'string' && /^\/upload\/waves\/(shadow|live)\//.test(url) && url.endsWith(suffix));
    const resolutionCalls = () => shadowCalls('/resolve-batch').flatMap(([url, body]) => body.items.map((item: any) => [url, item]));
    const opens = () => mocks.postUpload.mock.calls.filter(([url]) => /^\/upload\/waves\/(shadow|live)\/sessions$/.test(url));

    beforeEach(() => {
        vi.stubEnv('VITE_UPLOAD_WAVE_SHADOW_ENABLED', 'true');
        vi.stubGlobal('crypto', {
            randomUUID: () => `${(++waveUUIDSequence).toString(16).padStart(8, '0')}-1234-4234-8234-123456789abc`,
            subtle: { digest: async (_algorithm: string, bytes: Uint8Array) => {
                const hash = new Uint8Array(32);
                bytes.forEach((value, index) => { hash[index % 32] ^= value; });
                return hash.buffer;
            } },
        });
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            if (/^\/upload\/waves\/(shadow|live)\//.test(url)) {
                if (url.endsWith('/items')) return { manifestRevision: 'rev-items', registeredCount: payload.items.length };
                if (url.endsWith('/resolve-batch')) return { manifestRevision: 'rev-resolve' };
                if (url.endsWith('/discard')) return { state: 'abandoned', manifestRevision: 'server-preserved' };
                if (url.endsWith('/close')) return { state: 'closed' };
                return { waveId: 'wave-1', manifestRevision: 'rev-open' };
            }
            return original(url, payload);
        });
    });

    const failTwoOfSix = (blockItems: () => boolean) => {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.getUpload.mockImplementation(async (url) => url === '/upload/waves/protocol' ? { protocol: 'live' } : { hashes: {} });
        mocks.idbGet.mockImplementation(async () => new File(['x'], 'cached.heic'));
        const failing = new Set(['original-1.heic', 'original-3.heic']);
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            if (url.endsWith('/items') && blockItems()) throw { response: { data: { error: 'Library cleanup is in progress' } } };
            if (url === '/upload/finalize-batch') {
                return { results: payload.files.map((file: any) => failing.has(file.filename)
                    ? { filename: file.filename, error: 'Upload reservation failed' }
                    : { filename: file.filename, complete: true, duplicates: [] }) };
            }
            return original(url, payload);
        });
        return failing;
    };

    it('ignores repeated Retry clicks while the first retry is still starting', async () => {
        const failing = failTwoOfSix(() => false);
        select(filesFor(6));
        await advance(20000);
        expect(services.pendingUploadSummary?.fileCount).toBe(2);
        failing.clear();
        const opensBefore = opens().length;
        const finalizedBefore = finalizedFiles().length;
        act(() => { void services.retryPersistedUploadSession(); });
        expect(services.retryingUpload).toBe(true);
        act(() => { void services.retryPersistedUploadSession(); });
        act(() => { void services.retryPersistedUploadSession(); });
        await advance(20000);
        expect(opens().slice(opensBefore)).toHaveLength(1);
        expect(finalizedFiles().slice(finalizedBefore).map(file => file.filename).sort()).toEqual(['original-1.heic', 'original-3.heic']);
        expect(services.retryingUpload).toBe(false);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it.each([true, false])('a finalize that never gets its reply settles from the server\'s proof (stored=%s)', async (stored) => {
        // 2026-11-19: finalize stored the photo, then lost its wave binding
        // to contention and answered 503 on every retry. The browser called
        // the photo failed although the server held it.
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.getUpload.mockImplementation(async (url) => url === '/upload/waves/protocol' ? { protocol: 'live' } : { hashes: {} });
        mocks.idbGet.mockImplementation(async () => new File(['x'], 'cached.heic'));
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            if (url === '/upload/finalize-batch') throw { response: { status: 503, data: { code: 'wave_admission_retry' } } };
            if (url.endsWith('/resolve-batch') && stored && payload.items.some((item: any) => item.outcome === 'failed')) {
                return { manifestRevision: 'rev-adopted', adoptedItems: payload.items.map((item: any) => (
                    { itemId: item.itemId, outcome: 'accepted', filename: 'original-0-0123abcd.heic' })) };
            }
            return original(url, payload);
        });
        select(filesFor(1));
        await advance(180000);
        // Finalize really was retried to exhaustion before the browser asked.
        expect(finalizedFiles().length).toBeGreaterThanOrEqual(8);
        expect(resolutionCalls().filter(([, item]) => item.outcome === 'failed')).toHaveLength(1);
        const retrying = services.notifications.some(n => n.title === 'Retrying failed files');
        if (stored) {
            expect(retrying).toBe(false);
            expect(services.uploadActive).toBe(false);
            expect(services.pendingUploadSummary).toBeNull();
        } else {
            expect(retrying).toBe(true);
        }
    });

    it('resumes this device\'s upload from a notice about an earlier abandoned retry, after a reload', async () => {
        let blocked = false;
        const failing = failTwoOfSix(() => blocked);
        select(filesFor(6));
        await advance(20000);
        failing.clear();
        blocked = true;
        const opensBefore = opens().length;
        for (let attempt = 0; attempt < 2; attempt += 1) {
            act(() => { void services.retryPersistedUploadSession(); });
            await advance(4000);
        }
        const [first, second] = opens().slice(opensBefore).map(([, body]) => body.sessionId);
        expect(second).toBeDefined();
        blocked = false;
        // Reload, then Core reports both abandoned retries.
        cleanup();
        uploadWaveSync.__resetWaveShadowStateForTests();
        mocks.post.mockImplementation(async (url: string) => url === '/api/upload-sessions/reconcile'
            ? { notifications: [first, second].map(sessionId => ({ sessionId, waveId: 'w', affectedCount: 2, closedAt: Date.now() / 1000 })) }
            : {});
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(1000);
        const finalizedBefore = finalizedFiles().length;
        act(() => { void services.retryPersistedUploadSession(first); });
        await advance(20000);
        expect(services.notifications.some(n => n.title === 'Reselect unfinished files')).toBe(false);
        expect(opens().at(-1)?.[1]).toMatchObject({ declaredCount: 2, resumedFromSessionId: second });
        expect(finalizedFiles().slice(finalizedBefore).map(file => file.filename).sort()).toEqual(['original-1.heic', 'original-3.heic']);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('hands a resumed run\'s lane sizing the last measured latency and only the files still to send', async () => {
        const failing = failTwoOfSix(() => false);
        select(filesFor(6));
        await advance(20000);
        failing.clear();
        mocks.profile.mockClear();
        act(() => { void services.retryPersistedUploadSession(); });
        await advance(20000);
        expect(mocks.profile).toHaveBeenCalledTimes(1);
        const [files, options] = mocks.profile.mock.calls[0];
        expect(files).toHaveLength(2);
        expect(options).toHaveProperty('measuredRttMs');
    });

    it('opens, registers, resolves, and closes the shadow session for a normal completed upload', async () => {
        select(filesFor(2));
        await advance(4000);
        expect(opens()).toHaveLength(1);
        expect(opens()[0][1]).toMatchObject({ declaredCount: 2 });
        const registerCalls = shadowCalls('/items');
        expect(registerCalls).toHaveLength(1);
        expect(registerCalls[0][1].items).toHaveLength(2);
        // Cleanup reuses acknowledged outcomes instead of resolving them again.
        const resolveCalls = resolutionCalls();
        expect(resolveCalls.every(([, body]) => body.outcome === 'accepted')).toBe(true);
        expect(resolveCalls).toHaveLength(2);
        expect(new Set(resolveCalls.map(([, body]) => body.itemId)).size).toBe(2);
        expect(shadowCalls('/close')).toHaveLength(1);
    });

    it('declares one 901-file selection, registered batch by batch alongside init, before the first init', async () => {
        select(filesFor(901));
        for (let tick = 0; tick < 20 && finalizedFiles().length < 901; tick += 1) await advance(1000);
        await advance(2000);
        expect(finalizedFiles()).toHaveLength(901);
        expect(opens()).toHaveLength(1);
        expect(opens()[0][1]).toMatchObject({ declaredCount: 901 });
        // No separate bulk pass anymore -- each /items call registers exactly
        // one init-batch's worth of files (<= INIT_BATCH_MAX_SIZE), piggybacked
        // on that same batch instead of a fixed page size of its own.
        const registerSizes = shadowCalls('/items').map(([, body]) => body.items.length);
        expect(registerSizes.every((size) => size > 0 && size <= 24)).toBe(true);
        expect(registerSizes.reduce((sum, size) => sum + size, 0)).toBe(901);
        expect(shadowCalls('/close')).toHaveLength(1);
        const calls = mocks.postUpload.mock.calls;
        expect(calls.findIndex(([url]) => url.endsWith('/items')))
            .toBeLessThan(calls.findIndex(([url]) => url === '/upload/init-batch'));
        expect(new Set(resolutionCalls().map(([, body]) => body.itemId)).size).toBe(901);
    });

    it('pauses a blocked live selection once without failing or cancelling its files, then retries', async () => {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.getUpload.mockImplementation(async (url) => url === '/upload/waves/protocol' ? { protocol: 'live' } : { hashes: {} });
        const original = mocks.postUpload.getMockImplementation()!;
        let blocked = true;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            if (blocked && url.endsWith('/items')) {
                throw { response: { data: { error: 'Library cleanup is in progress' } } };
            }
            return original(url, payload);
        });
        select(filesFor(50));
        await advance(4000);
        expect(services.uploading).toBe(false);
        expect(services.notifications.some(notification => notification.details?.includes('Library cleanup is in progress'))).toBe(true);
        expect(services.pendingUploadSummary).toMatchObject({ fileCount: 50, failedCount: 0 });
        expect(mocks.transferred).not.toHaveBeenCalled();
        expect(resolutionCalls()).toHaveLength(0);
        expect(shadowCalls('/close')).toHaveLength(0);
        blocked = false;
        act(() => { void services.retryPersistedUploadSession(); });
        await advance(5000);
        expect(finalizedFiles()).toHaveLength(50);
        expect(services.pendingUploadSummary).toBeNull();
    });

    it('starts transfers after a bounded live admission window while the remaining manifest registers', async () => {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.getUpload.mockImplementation(async url => url === '/upload/waves/protocol' ? {protocol:'live'} : {hashes:{}});
        const original=mocks.postUpload.getMockImplementation()!;
        let itemCalls=0;
        let release!: () => void;
        const gate=new Promise<void>(resolve => {release=resolve;});
        mocks.postUpload.mockImplementation(async (url,payload) => {
            if (url.endsWith('/items') && ++itemCalls===2) await gate;
            return original(url,payload);
        });
        select(filesFor(450));
        await advance(4000);
        expect(shadowCalls('/items').length).toBeGreaterThanOrEqual(2);
        expect(shadowCalls('/items')[0][1].items).toHaveLength(48);
        expect(shadowCalls('/items')[1][1].items.length).toBeLessThanOrEqual(192);
        expect(initFiles().length).toBeGreaterThan(0);
        expect(mocks.transferred).toHaveBeenCalled();
        expect(shadowCalls('/close')).toHaveLength(0);
        await act(async () => {release();});
        for (let tick=0;tick<40 && finalizedFiles().length<450;tick++) await advance(1000);
        await advance(2000);
        const registered=shadowCalls('/items').flatMap(([,body]) => body.items);
        expect(new Set(registered.map(item => item.filename))).toEqual(new Set(filesFor(450).map(file => file.name)));
        expect(new Set(registered.map(item => item.itemId)).size).toBe(450);
        expect(finalizedFiles()).toHaveLength(450);
        expect(opens()).toHaveLength(1);
        expect(shadowCalls('/items').length).toBeGreaterThanOrEqual(2);
    });

    it('deduplicates before declaring the live count or registering upload items', async () => {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        const files = filesFor(50);
        const duplicateHashes = Object.fromEntries(files.slice(0, 20).map(file => [
            Array.from(new TextEncoder().encode(file.name)).join('-'),
            `stored-${file.name}`,
        ]));
        mocks.getUpload.mockImplementation(async url => url === '/upload/waves/protocol'
            ? { protocol: 'live' }
            : { hashes: duplicateHashes });

        select(files);
        for (let tick = 0; tick < 40 && finalizedFiles().length < 30; tick += 1) await advance(1000);
        await advance(2000);

        const registered = shadowCalls('/items').flatMap(([, body]) => body.items);
        expect(opens()).toHaveLength(1);
        expect(opens()[0][1]).toMatchObject({ declaredCount: 30 });
        expect(registered).toHaveLength(30);
        expect(initFiles()).toHaveLength(30);
        expect(mocks.postUpload.mock.calls.some(([url]) => url.endsWith('/duplicate-exclusions'))).toBe(false);
        expect(services.notifications.some(notification =>
            notification.progress?.skippedDuplicateCount === 20)).toBe(true);
    });

    it('heartbeat expiry aborts active live transfer intake and retains an explicit retry checkpoint without opening a successor', async () => {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.getUpload.mockImplementation(async url => url === '/upload/waves/protocol' ? {protocol:'live'} : {hashes:{}});
        const original=mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url,payload) => {
            if (url === '/upload/transfer-heartbeat') return {leaseActive:false};
            return original(url,payload);
        });
        mocks.transferred.mockImplementation((_id,_data,_size,options) => new Promise((_resolve,reject) => {
            const abort=() => reject(new Error('upload_stopped_by_user'));
            if (options.abortSignal.aborted) abort();
            else options.abortSignal.addEventListener('abort',abort,{once:true});
        }));
        select(filesFor(50));
        await advance(1000);
        expect(mocks.transferred).toHaveBeenCalled();
        const admitted=opens()[0][1].sessionId;
        await advance(95000);
        expect(services.uploading).toBe(false);
        expect(services.pendingUploadSummary).not.toBeNull();
        expect(services.notifications.some(notification => notification.title === 'Upload expired')).toBe(true);
        expect(opens()).toHaveLength(1);
        expect(shadowCalls('/discard')[0][0]).toContain(admitted);
        expect(finalizedFiles()).toHaveLength(0);
    });

    it('stops retrying (auto or manual) once a session hits the attempt cap, instead of retrying forever', async () => {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.getUpload.mockImplementation(async (url) => url === '/upload/waves/protocol' ? { protocol: 'live' } : { hashes: {} });
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            if (url.endsWith('/items')) throw { response: { data: { error: 'Library cleanup is in progress' } } };
            return original(url, payload);
        });
        select(filesFor(1));
        await advance(4000);
        expect(services.pendingUploadSummary).not.toBeNull();
        expect(resolutionCalls()).toHaveLength(0);
        // Every manual Retry click (same call the banner's button makes) keeps
        // hitting the same blocked /items call, consuming one attempt each --
        // exactly what a user repeatedly clicking Retry on a stuck session
        // does. The initial select() above isn't itself a "retry", so this
        // loop is what exhausts all MAX_UPLOAD_RETRY_ATTEMPTS attempts.
        for (let attempt = 0; attempt < MAX_UPLOAD_RETRY_ATTEMPTS; attempt += 1) {
            act(() => { void services.retryPersistedUploadSession(); });
            await advance(4000);
        }
        // Each top-level retry call can itself issue more than one raw /items
        // request via the existing HTTP-level retry wrapper, so this count is
        // not expected to equal MAX_UPLOAD_RETRY_ATTEMPTS -- the cap tracks
        // retryPersistedUploadSession invocations, not raw HTTP attempts. What
        // matters is that it stops growing once the cap is hit, below.
        const itemsCallsBeforeCap = shadowCalls('/items').length;
        act(() => { void services.retryPersistedUploadSession(); });
        await advance(4000);
        // The capped retry must not have touched the server at all.
        expect(shadowCalls('/items')).toHaveLength(itemsCallsBeforeCap);
        expect(services.notifications.some(notification => notification.title === 'Retry limit reached')).toBe(true);
        expect(services.pendingUploadSummary).not.toBeNull();
    });

    it('uses live finalize acknowledgements without extra outcome or close calls', async () => {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.getUpload.mockImplementation(async (url) => url === '/upload/waves/protocol' ? { protocol: 'live' } : { hashes: {} });
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            const result = await original(url, payload);
            if (url === '/upload/finalize-batch') {
                result.results.forEach((entry: any, index: number) => {
                    entry.waveResolution = { sessionId: payload.files[index].waveSessionId,
                        itemId: payload.files[index].waveItemId, manifestRevision: 'finalized', state: 'closed' };
                });
            }
            return result;
        });
        select(filesFor(2));
        await advance(4000);
        expect(finalizedFiles()).toHaveLength(2);
        // Registration now rides along with each init-batch flush rather
        // than a single upfront bulk call -- 2 small files may land in one
        // batch or two depending on debounce timing; what matters is both
        // got registered, not the exact call count.
        expect(shadowCalls('/items').flatMap(([, body]) => body.items)).toHaveLength(2);
        expect(shadowCalls('/resolve-batch')).toHaveLength(0);
        expect(shadowCalls('/close')).toHaveLength(0);
    });

    it.each([[50, 'shadow'], [901, 'shadow'], [50, 'live'], [901, 'live']] as const)(
        'closes a stopped %i-file %s selection while preserving accepted photos', async (count, protocol) => {
        if (protocol === 'live') {
            vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
            mocks.getUpload.mockImplementation(async (url) => url === '/upload/waves/protocol' ? { protocol: 'live' } : { hashes: {} });
        }
        mocks.transferred.mockResolvedValueOnce(undefined).mockImplementation((_id, _data, _size, options) =>
            new Promise((_resolve, reject) => {
                const abort = () => reject(new Error('upload_stopped_by_user'));
                if (options.abortSignal.aborted) abort();
                else options.abortSignal.addEventListener('abort', abort, { once: true });
            }));
        select(filesFor(count));
        await advance(2000);
        expect(finalizedFiles()).toHaveLength(1);
        act(() => services.stopActiveUpload());
        await advance(4000);
        const outcomes = new Map(resolutionCalls().map(([, body]) => [body.itemId, body.outcome]));
        if (protocol === 'live') {
            expect(shadowCalls('/discard')).toHaveLength(1);
            expect(Array.from(outcomes.values())).not.toContain('cancelled');
            expect(shadowCalls('/close')).toHaveLength(0);
            expect(finalizedFiles()).toHaveLength(1);
        } else {
            // Shadow (advisory) registration now happens lazily per
            // init-batch rather than a single upfront bulk call -- by the
            // time Stop runs, only whatever batches already flushed are
            // registered (<= count), and only those get resolved. Files
            // whose batch never ran are simply never declared, consistent
            // with "unregistered declarations are excluded."
            expect(outcomes.size).toBeGreaterThan(0);
            expect(outcomes.size).toBeLessThanOrEqual(count);
            expect(Array.from(outcomes.values()).filter(outcome => outcome === 'accepted')).toHaveLength(1);
            expect(Array.from(outcomes.values()).filter(outcome => outcome === 'cancelled')).toHaveLength(outcomes.size - 1);
        }
        expect(services.uploading).toBe(false);
    });

    it.each(['shadow', 'live'])('does not cancel committed photos while their %s finalization is still pending on Stop', async (protocol) => {
        if (protocol === 'live') {
            vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
            mocks.getUpload.mockImplementation(async (url) => url === '/upload/waves/protocol' ? { protocol: 'live' } : { hashes: {} });
        }
        let releaseFinalize!: () => void;
        const gate = new Promise<void>(resolve => { releaseFinalize = resolve; });
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url, payload) => {
            if (url === '/upload/finalize-batch') await gate;
            return original(url, payload);
        });
        mocks.transferred.mockResolvedValueOnce(undefined).mockImplementation((_id, _data, _size, options) =>
            new Promise((_resolve, reject) => {
                const abort = () => reject(new Error('upload_stopped_by_user'));
                if (options.abortSignal.aborted) abort();
                else options.abortSignal.addEventListener('abort', abort, { once: true });
            }));
        select(filesFor(50));
        await advance(2000);
        const committedFilename = finalizedFiles()[0].filename;
        act(() => services.stopActiveUpload());
        await advance(3000);
        const cancelledFiles = () => mocks.postUpload.mock.calls.filter(([url]) => url === '/upload/cancel')
            .flatMap(([, body]) => body.files);
        expect(cancelledFiles().some(file => file.filename === committedFilename)).toBe(false);
        expect(shadowCalls('/close')).toHaveLength(0);
        await act(async () => { releaseFinalize(); });
        await advance(4000);
        expect(cancelledFiles().length).toBeGreaterThan(0);
        expect(cancelledFiles().some(file => file.filename === committedFilename)).toBe(false);
        if (protocol === 'live') {
            expect(shadowCalls('/discard')).toHaveLength(1);
            expect(shadowCalls('/close')).toHaveLength(0);
        } else {
            // Shadow close() requires every declared item resolved
            // (waveParentResolved), which a stopped selection with most
            // batches never registered can no longer satisfy -- the
            // meaningful guarantee here is that the committed file still
            // resolves as accepted, not a shadow-mode close.
            expect(resolutionCalls().some(([, body]) => body.outcome === 'accepted')).toBe(true);
        }
    });

    it('resolves a finalize failure as failed, not accepted', async () => {
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            if (url === '/upload/finalize-batch') throw new Error('finalize failed');
            return original(url, payload);
        });
        select(filesFor(1));
        for (let tick = 0; tick < 8; tick += 1) await advance(1000);
        const resolveCalls = resolutionCalls();
        expect(resolveCalls.some(([, body]) => body.outcome === 'failed')).toBe(true);
    });

    it('never affects the real upload path when the shadow API is unavailable', async () => {
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation(async (url: string, payload: any) => {
            if (url.startsWith('/upload/waves/shadow/')) throw new Error('shadow disabled');
            return original(url, payload);
        });
        select(filesFor(2));
        await advance(4000);
        expect(finalizedFiles()).toHaveLength(2);
        expect(services.uploading).toBe(false);
        expect(services.pendingUploadSummary).toBeNull();
    });
});


describe('discard acknowledgment after refresh', () => {
    const restorePaused = () => {
        const file = filesFor(1)[0];
        localStorage.setItem(UPLOAD_SESSION_STORAGE_KEY, JSON.stringify({
            id: 'discard-restored', createdAt: Date.now(), files: [{
                key: 'discard-restored:0', name: file.name, size: file.size, type: file.type,
                lastModified: file.lastModified, uploadedBytes: 0, skippedDuplicate: false, status: 'pending',
            }],
        }));
        render(<AppServicesProvider><Probe /></AppServicesProvider>);
    };

    it('holds the checkpoint and new upload admission until server discard succeeds', async () => {
        let release!: () => void;
        vi.spyOn(uploadWaveSync, 'discardWaveShadowSession').mockImplementation(() => new Promise<void>(resolve => { release = resolve; }));
        restorePaused(); await advance(100);
        let discarding!: Promise<void>;
        act(() => { discarding = services.discardPersistedUploadSession(); });
        await advance(0);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).not.toBeNull();
        expect(services.pendingUploadSummary).not.toBeNull();
        await act(async () => { await services.startUpload(filesFor(2)); await services.retryPersistedUploadSession(); });
        expect(initFiles()).toHaveLength(0);
        await act(async () => { release(); await discarding; });
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
        expect(services.pendingUploadSummary).toBeNull();
        act(() => { void services.startUpload(filesFor(2)); });
        await advance(5000);
        expect(finalizedFiles()).toHaveLength(2);
    });

    it('keeps recovery information after failed discard and allows a later retry', async () => {
        const discard = vi.spyOn(uploadWaveSync, 'discardWaveShadowSession')
            .mockRejectedValueOnce(new Error('Server discard could not finish'))
            .mockResolvedValueOnce(undefined);
        restorePaused(); await advance(100);
        await act(async () => { await services.discardPersistedUploadSession(); });
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).not.toBeNull();
        expect(services.pendingUploadSummary).not.toBeNull();
        expect(services.notifications.some(n => n.title === 'Upload discarded')).toBe(false);
        await act(async () => { await services.discardPersistedUploadSession(); });
        expect(discard).toHaveBeenCalledTimes(2);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
    });

    it('clears the paused upload locally when the session was rejected, so new uploads are not blocked', async () => {
        vi.spyOn(uploadWaveSync, 'discardWaveShadowSession')
            .mockRejectedValueOnce(Object.assign(new Error('Invalid or expired session'), { status: 401 }));
        restorePaused(); await advance(100);
        await act(async () => { await services.discardPersistedUploadSession(); });
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.notifications.some(n => n.title === 'Upload discard failed')).toBe(false);
        act(() => { void services.startUpload(filesFor(2)); });
        await advance(5000);
        expect(finalizedFiles()).toHaveLength(2);
    });

    it('tells the user a blocked discard is a temporary server-side wait, not a failure', async () => {
        // Mirrors the exact wording of backend/wave_settlement.py's abandon():
        // raise Conflict('A finalization is still in flight; retry abandonment later')
        vi.spyOn(uploadWaveSync, 'discardWaveShadowSession')
            .mockRejectedValueOnce(new Error('Upload admission failed: A finalization is still in flight; retry abandonment later'));
        restorePaused(); await advance(100);
        await act(async () => { await services.discardPersistedUploadSession(); });
        expect(services.notifications.some(n =>
            n.title === 'Still finishing on the server' && n.details?.includes('Wait a few minutes'))).toBe(true);
        expect(services.notifications.some(n => n.title === 'Upload discard failed')).toBe(false);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).not.toBeNull();
    });

    it('a late finalize callback cannot erase the checkpoint while server discard is pending', async () => {
        let finishDiscard!: () => void;
        let finishFinalize!: (value: unknown) => void;
        vi.spyOn(uploadWaveSync, 'discardWaveShadowSession').mockImplementation(() => new Promise<void>(resolve => { finishDiscard = resolve; }));
        const original = mocks.postUpload.getMockImplementation()!;
        mocks.postUpload.mockImplementation((url: string, payload: any) => {
            if (url === '/upload/finalize-batch') return new Promise(resolve => { finishFinalize = resolve; });
            return original(url, payload);
        });
        select(filesFor(1)); await advance(2000);
        expect(finishFinalize).toBeTypeOf('function');
        expect(services.uploading).toBe(false);
        let discarding!: Promise<void>;
        act(() => { discarding = services.discardPersistedUploadSession(); });
        await advance(0);
        expect(finishDiscard).toBeTypeOf('function');
        await act(async () => { finishFinalize({results: [{filename: 'original-0.heic', complete: true, duplicates: []}]}); });
        await advance(500);
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).not.toBeNull();
        expect(services.pendingUploadSummary).not.toBeNull();
        await act(async () => { finishDiscard(); await discarding; });
        expect(localStorage.getItem(UPLOAD_SESSION_STORAGE_KEY)).toBeNull();
    });

});


describe('Core stale-upload notifications', () => {
    it('shows the durable notice outside Tools, deduplicates and acknowledges it when read', async () => {
        mocks.post.mockImplementation(async (url: string) => url === '/api/upload-sessions/reconcile'
            ? { notifications: [{ sessionId: 'closed-parent', waveId: 'old-wave', affectedCount: 7, closedAt: Date.now()/1000 }] }
            : {});
        render(<AppServicesProvider><Probe toolsPage={false} /><NotificationBell /></AppServicesProvider>);
        await advance(0);
        expect(services.notifications.filter(n => n.uploadSessionId === 'closed-parent')).toHaveLength(1);
        expect(services.notifications.find(n => n.uploadSessionId === 'closed-parent')?.details).toContain('7 files did not finish');
        expect(mocks.jobs).not.toHaveBeenCalled();
        await advance(61000);
        await act(async () => { window.dispatchEvent(new Event('focus')); });
        expect(services.notifications.filter(n => n.uploadSessionId === 'closed-parent')).toHaveLength(1);
        await act(async () => { fireEvent.click(screen.getByLabelText(/^Open notifications/)); });
        await act(async () => { fireEvent.click(screen.getByText('Retry unfinished files')); });
        expect(services.notifications.some(n => n.title === 'Reselect unfinished files')).toBe(true);
        expect(mocks.post).toHaveBeenCalledWith('/api/upload-sessions/notifications/closed-parent/ack', {}, expect.any(Object));
    });

    it('does not poll Core indefinitely for healthy sessions', async () => {
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        await advance(180000);
        expect(mocks.post.mock.calls.filter(([url]) => url === '/api/upload-sessions/reconcile')).toHaveLength(1);
    });

    it('offers re-selection when a notice has no matching local retry checkpoint', async () => {
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await act(async () => { await services.retryPersistedUploadSession('another-device-session'); });
        expect(services.notifications.some(n => n.title === 'Reselect unfinished files')).toBe(true);
        expect(mocks.postUpload).not.toHaveBeenCalledWith('/upload/init-batch', expect.anything(), expect.anything());
    });
});


it('ends Core recovery follow-ups when pending cleanup drains', async () => {
    let checks = 0;
    mocks.post.mockImplementation(async (url: string) => url === '/api/upload-sessions/reconcile'
        ? { pending: ++checks === 1, notifications: [] } : {});
    render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
    await advance(90000);
    expect(checks).toBe(2);
});


describe('Server activity in the bell', () => {
    it('turns a shared-album open into one notification, and not again after a reload', async () => {
        const openedAt = Date.now() / 1000 - 120;
        mocks.get.mockImplementation(async (url: string) => url === '/api/activity'
            ? { albumOpens: [{ albumId: 'a1', name: 'Malmö nights', openedAt, opens: 1 }], invitesEnding: [] }
            : {});
        const first = render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        expect(services.notifications.filter((n) => n.kind === 'share').map((n) => n.title)).toEqual(['Someone opened “Malmö nights”']);
        first.unmount();
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        expect(services.notifications.some((n) => n.kind === 'share')).toBe(false);
    });

    it('fills in how processing went once the upload’s wave completes', async () => {
        let done = false;
        mocks.get.mockImplementation(async (url: string) => url === '/api/upload-sessions/s1/processing'
            ? (done ? { done: true, processedSeconds: 240, failed: 0, people: 9 } : { done: false, state: 'processing' })
            : {});
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        let id = '';
        act(() => { id = services.addNotification('31 items uploaded', 'They show in Photos once processed.'); });
        act(() => { services.updateNotification(id, { processing: { sessionId: 's1', since: Date.now() } }); });
        await advance(120_000);
        expect(services.notifications.find((n) => n.id === id)?.processing).toBeDefined();
        done = true;
        await advance(120_000);
        const entry = services.notifications.find((n) => n.id === id);
        expect(entry?.details).toBe('All processed in 4 minutes. 9 people found.');
        expect(entry?.processing).toBeUndefined();
        const calls = mocks.get.mock.calls.filter(([url]) => url === '/api/upload-sessions/s1/processing').length;
        await advance(120_000);
        expect(mocks.get.mock.calls.filter(([url]) => url === '/api/upload-sessions/s1/processing')).toHaveLength(calls);
    });

    it('shows one Processing photos entry that becomes the final summary, without adding another', async () => {
        let done = false;
        mocks.get.mockImplementation(async (url: string) => url === '/api/upload-sessions/s1/processing'
            ? (done ? { done: true, processedSeconds: 240, failed: 0, people: 2 } : { done: false, state: 'processing' })
            : {});
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        let id = '';
        act(() => {
            id = services.addNotification('Processing photos', '31 items uploaded. Processing on the server.');
            services.updateNotification(id, { processing: { sessionId: 's1', since: Date.now(), restore: { title: '31 items uploaded', details: 'They show in Photos once processed.' } } });
        });
        await advance(120_000);
        expect(services.notifications.filter((n) => n.id === id)).toHaveLength(1);
        expect(services.notifications.find((n) => n.id === id)?.title).toBe('Processing photos');
        const before = services.notifications.length;
        done = true;
        await advance(120_000);
        const entry = services.notifications.find((n) => n.id === id);
        expect(entry).toMatchObject({ title: '31 items uploaded', details: 'All processed in 4 minutes. 2 people found.' });
        expect(entry?.processing).toBeUndefined();
        expect(services.notifications).toHaveLength(before);
    });

    it('puts the upload wording back when the watch ends without a summary', async () => {
        mocks.get.mockImplementation(async (url: string) => {
            if (url === '/api/upload-sessions/legacy/processing') throw Object.assign(new Error('nope'), { response: { status: 404 } });
            return {};
        });
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        let id = '';
        act(() => {
            id = services.addNotification('Processing photos', 'x');
            services.updateNotification(id, { processing: { sessionId: 'legacy', since: Date.now(), restore: { title: '3 items uploaded', details: 'Done.' } } });
        });
        await advance(0);
        expect(services.notifications.find((n) => n.id === id)).toMatchObject({ title: '3 items uploaded', details: 'Done.' });
    });

    it('reads the processing summary on a push instead of polling while the socket is up', async () => {
        let done = false;
        mocks.get.mockImplementation(async (url: string) => url === '/api/upload-sessions/s1/processing'
            ? (done ? { done: true, processedSeconds: 60, failed: 0, people: null } : { done: false, state: 'processing' })
            : {});
        const reads = () => mocks.get.mock.calls.filter(([url]) => url === '/api/upload-sessions/s1/processing').length;
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        act(() => { mocks.push?.onConnection?.(true); });
        let id = '';
        act(() => {
            id = services.addNotification('Processing photos', 'x');
            services.updateNotification(id, { processing: { sessionId: 's1', since: Date.now(), restore: { title: '5 items uploaded', details: 'd' } } });
        });
        await advance(0);
        expect(reads()).toBe(1);
        await advance(8 * 60_000);
        expect(reads()).toBe(1); // no 2-minute polling while the push is connected
        done = true;
        await act(async () => { mocks.push?.onProcessing?.(); });
        await advance(0);
        expect(reads()).toBe(2);
        expect(services.notifications.find((n) => n.id === id)).toMatchObject({ title: '5 items uploaded', details: 'All processed in 1 minute.' });
    });

    it('falls back to slower polling once the push disconnects', async () => {
        mocks.get.mockImplementation(async (url: string) => url === '/api/upload-sessions/s1/processing'
            ? { done: false, state: 'processing' } : {});
        const reads = () => mocks.get.mock.calls.filter(([url]) => url === '/api/upload-sessions/s1/processing').length;
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        act(() => { mocks.push?.onConnection?.(true); });
        let id = '';
        act(() => {
            id = services.addNotification('Processing photos', 'x');
            services.updateNotification(id, { processing: { sessionId: 's1', since: Date.now() } });
        });
        await advance(0);
        act(() => { mocks.push?.onConnection?.(false); });
        const before = reads();
        await advance(60_000);
        expect(reads()).toBe(before); // slower than the old 30 s
        await advance(60_000);
        expect(reads()).toBe(before + 1);
    });

    it('stops asking when the server does not know the upload', async () => {
        mocks.get.mockImplementation(async (url: string) => {
            if (url === '/api/upload-sessions/legacy/processing') throw Object.assign(new Error('nope'), { response: { status: 404 } });
            return {};
        });
        render(<AppServicesProvider><Probe toolsPage={false} /></AppServicesProvider>);
        await advance(0);
        let id = '';
        act(() => { id = services.addNotification('3 items uploaded', ''); });
        act(() => { services.updateNotification(id, { processing: { sessionId: 'legacy', since: Date.now() } }); });
        await advance(0);
        const entry = services.notifications.find((n) => n.id === id);
        expect(entry?.processing).toBeUndefined();
        expect(entry?.details).toBe('');
    });
});

describe('upload lanes versus the preparation gate', () => {
    const FILES = 300;
    const TRANSFER_MS = 1500;
    const PREPARE_MS = 2000;
    const GATE = 5;

    // Virtual-time stand-ins: 20 lanes, bytes take TRANSFER_MS, preparation runs GATE at a time for PREPARE_MS.
    function emulate() {
        mocks.preparationEnabled = true;
        mocks.profile.mockImplementation(() => ({ fileParallelism: 20, chunkSizeBytes: 4 * 1024 * 1024, reason: 'emulated' }));
        mocks.transferred.mockImplementation(() => new Promise<void>((resolve) => { setTimeout(resolve, TRANSFER_MS); }));
        const stats = { active: 0, waiting: 0, peakWaiting: 0 };
        const queue: Array<() => void> = [];
        const release = () => { stats.active -= 1; const next = queue.shift(); if (next) { stats.waiting -= 1; next(); } };
        mocks.prepared.mockImplementation(() => new Promise((resolve) => {
            const run = () => { stats.active += 1; setTimeout(() => { release(); resolve({}); }, PREPARE_MS); };
            if (stats.active < GATE) run();
            else { stats.waiting += 1; stats.peakWaiting = Math.max(stats.peakWaiting, stats.waiting); queue.push(run); }
        }));
        return stats;
    }

    it('keeps the lanes from running far ahead of preparation without slowing the upload', async () => {
        const stats = emulate();
        select(filesFor(FILES));
        let seconds = 0;
        while (finalizedFiles().length < FILES && seconds < 600) { await advance(1000); seconds += 1; }
        expect(finalizedFiles()).toHaveLength(FILES);
        expect(stats.peakWaiting).toBeLessThanOrEqual(40);
        // The gate is the long pole: FILES x PREPARE_MS / GATE, plus the first transfer and the last finalize.
        expect(seconds).toBeLessThanOrEqual(FILES * PREPARE_MS / GATE / 1000 + 10);
    }, 60000);

    it('pausing lands within seconds, not after the whole preparation queue drains', async () => {
        emulate();
        select(filesFor(FILES));
        await advance(30_000);
        act(() => { services.pauseActiveUpload(); });
        let seconds = 0;
        while ((services.uploadActive || services.pausingUpload) && seconds < 600) { await advance(1000); seconds += 1; }
        expect(services.uploadActive).toBe(false);
        expect(seconds).toBeLessThanOrEqual(30);
    }, 60000);
});
