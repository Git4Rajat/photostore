import React from 'react';
import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
    getUpload: vi.fn(),
    postUpload: vi.fn(),
    staged: vi.fn(),
    committed: vi.fn(),
    toast: vi.fn(),
    span: vi.fn(),
}));

vi.mock('../config/appConfig', () => ({ getRuntimeConfig: () => ({ processingMode: 'backend' }) }));
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
    isLikelyAuthenticated: () => false,
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
    idbGet: vi.fn().mockResolvedValue(null),
    idbDelete: vi.fn().mockResolvedValue(undefined),
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
    vi.stubGlobal('localStorage', memoryStorage());
    vi.stubGlobal('sessionStorage', memoryStorage());
    localStorage.clear();
    sessionStorage.clear();
    mocks.getUpload.mockImplementation(async () => ({ hashes: {} }));
    mocks.staged.mockResolvedValue(undefined);
    mocks.committed.mockResolvedValue(undefined);
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

describe('browser upload selection and startup', () => {
    it.each([300, 3000])('initializes and finalizes all %i selected originals', async (count) => {
        const files = filesFor(count);
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
        expect(services.pendingUploadSummary).toBeNull();
        expect(services.queuedUploadFileCount).toBe(0);
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
