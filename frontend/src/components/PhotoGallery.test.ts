import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPersistentBrowserAiWorker } from './PhotoGallery';
import type { BrowserAiModelState } from './PhotoGallery';

// createPersistentBrowserAiWorker() internally still builds an image payload via
// createBrowserAiImagePayload (createImageBitmap + canvas readback), same as the
// original per-call runBrowserAiVisionInWorker it replaces -- neither jsdom
// capability exists by default, so both are stubbed with minimal fakes. The
// pixel content is irrelevant to what these tests check (worker lifecycle and
// request/response correlation), only that the pipeline can run end-to-end.
class MockWorker {
    static instances: MockWorker[] = [];
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: ErrorEvent) => void) | null = null;
    terminated = false;
    postedMessages: any[] = [];

    constructor(public url: URL | string, public options?: WorkerOptions) {
        MockWorker.instances.push(this);
    }

    postMessage(data: any) {
        this.postedMessages.push(data);
    }

    terminate() {
        this.terminated = true;
    }

    reply(requestId: string, result: Record<string, any>) {
        this.onmessage?.({ data: { type: 'browser-ai-analyze-result', requestId, ok: true, result } } as MessageEvent);
    }

    replyError(requestId: string, reason: string) {
        this.onmessage?.({ data: { type: 'browser-ai-analyze-result', requestId, ok: false, reason } } as MessageEvent);
    }
}

const modelState: BrowserAiModelState = {
    status: 'available',
    modelAvailability: 'available',
    manifest: { model: 'test-model' },
};

const fakeImage = () => new Blob([new Uint8Array([1, 2, 3])], { type: 'image/jpeg' });

const flushMicrotasks = async (times = 8) => {
    for (let i = 0; i < times; i += 1) {
        await Promise.resolve();
    }
};

beforeEach(() => {
    MockWorker.instances = [];
    (globalThis as any).Worker = MockWorker;
    (globalThis as any).createImageBitmap = vi.fn().mockResolvedValue({ width: 2, height: 2, close: () => {} });
    HTMLCanvasElement.prototype.getContext = vi.fn().mockReturnValue({
        drawImage: () => {},
        getImageData: (_x: number, _y: number, w: number, h: number) => ({ data: new Uint8ClampedArray(Math.max(1, w) * Math.max(1, h) * 4) }),
    }) as any;
});

afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
});

describe('createPersistentBrowserAiWorker', () => {
    it('creates exactly one Worker and reuses it across multiple analyze() calls', async () => {
        const handle = createPersistentBrowserAiWorker();
        expect(MockWorker.instances.length).toBe(1);
        const worker = MockWorker.instances[0];

        const first = handle.analyze(fakeImage(), modelState, 5000);
        await flushMicrotasks();
        expect(worker.postedMessages.length).toBe(1);
        worker.reply(worker.postedMessages[0].requestId, { tags: ['a'] });
        await expect(first).resolves.toEqual({ tags: ['a'] });

        const second = handle.analyze(fakeImage(), modelState, 5000);
        await flushMicrotasks();
        expect(worker.postedMessages.length).toBe(2);
        worker.reply(worker.postedMessages[1].requestId, { tags: ['b'] });
        await expect(second).resolves.toEqual({ tags: ['b'] });

        // The core "conveyor belt" guarantee: still exactly one Worker across
        // both photos, not a fresh one per analyze() call.
        expect(MockWorker.instances.length).toBe(1);
        expect(worker.terminated).toBe(false);
        handle.dispose();
    });

    it('rejects analyze() immediately when the manifest is missing, without posting to the worker', async () => {
        const handle = createPersistentBrowserAiWorker();
        const worker = MockWorker.instances[0];
        await expect(handle.analyze(fakeImage(), { ...modelState, manifest: undefined }, 5000))
            .rejects.toThrow('model_unavailable');
        await flushMicrotasks();
        expect(worker.postedMessages.length).toBe(0);
        handle.dispose();
    });

    it('rejects with the reason reported by a failed analyze-result message', async () => {
        const handle = createPersistentBrowserAiWorker();
        const worker = MockWorker.instances[0];
        const pending = handle.analyze(fakeImage(), modelState, 5000);
        await flushMicrotasks();
        worker.replyError(worker.postedMessages[0].requestId, 'classifier_unavailable');
        await expect(pending).rejects.toThrow('classifier_unavailable');
        handle.dispose();
    });

    it('rejects with inference_timeout when no reply arrives in time', async () => {
        vi.useFakeTimers();
        const handle = createPersistentBrowserAiWorker();
        const promise = handle.analyze(fakeImage(), modelState, 1000);
        // Attach the rejection assertion before advancing timers, so the
        // .rejects handler is already in place when the timeout fires --
        // otherwise the rejection races vi.advanceTimersByTimeAsync and
        // surfaces as an unhandled rejection instead of a caught one.
        const assertion = expect(promise).rejects.toThrow('inference_timeout');
        await vi.advanceTimersByTimeAsync(1000);
        await assertion;
        handle.dispose();
    });

    it('rejects all pending analyze() calls and terminates the worker on dispose()', async () => {
        const handle = createPersistentBrowserAiWorker();
        const pending = handle.analyze(fakeImage(), modelState, 5000);
        await flushMicrotasks();
        handle.dispose();
        await expect(pending).rejects.toThrow('worker_disposed');
        expect(MockWorker.instances[0].terminated).toBe(true);
    });

    it('rejects all pending analyze() calls when the worker itself errors', async () => {
        const handle = createPersistentBrowserAiWorker();
        const pending = handle.analyze(fakeImage(), modelState, 5000);
        await flushMicrotasks();
        const worker = MockWorker.instances[0];
        worker.onerror?.({ message: 'boom' } as ErrorEvent);
        await expect(pending).rejects.toThrow('boom');
        handle.dispose();
    });
});
