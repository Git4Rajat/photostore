import {afterEach, beforeEach, describe, expect, test, vi} from 'vitest';

class FakeWorker {
    static instances: FakeWorker[] = [];
    static respond: (worker: FakeWorker, message: {id: number; name: string}) => void = (worker, message) => {
        worker.reply({id: message.id, ok: true, json: {'File:FileType': 'JPEG', 'EXIF:Make': 'Test'}});
    };
    onmessage: ((event: {data: unknown}) => void) | null = null;
    onerror: (() => void) | null = null;
    terminated = false;
    received: Array<{id: number; name: string; type: string}> = [];
    constructor(public url: string) { FakeWorker.instances.push(this); }
    postMessage(message: {id: number; name: string; type: string}) {
        if (message.type !== 'parse') return;
        this.received.push(message);
        FakeWorker.respond(this, message);
    }
    terminate() { this.terminated = true; }
    reply(data: unknown) { this.onmessage?.({data}); }
}

async function loadClient() {
    vi.resetModules();
    return import('./browserExifTool');
}

const bytes = () => new Uint8Array([1, 2, 3]);
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

beforeEach(() => {
    FakeWorker.instances = [];
    FakeWorker.respond = (worker, message) => {
        worker.reply({id: message.id, ok: true, json: {'File:FileType': 'JPEG'}});
    };
    vi.stubGlobal('Worker', FakeWorker);
});

afterEach(() => {
    vi.unstubAllGlobals();
});

describe('browser EXIF client', () => {
    test('returns the verified browser verdict for a clean parse', async () => {
        const {extractBrowserExif} = await loadClient();
        const outcome = await extractBrowserExif(bytes(), 'IMG_0001.jpg');
        expect(outcome).toEqual({
            ok: true,
            result: {source: 'browser_exiftool', verifiedComplete: true, exifToolJson: {'File:FileType': 'JPEG'}},
        });
        expect(FakeWorker.instances[0].received[0].name).toBe('IMG_0001.jpg');
    });

    test('runs requests one at a time so a recycle never fails a neighbour', async () => {
        const {extractBrowserExif} = await loadClient();
        const replies: Array<() => void> = [];
        FakeWorker.respond = (worker, message) => {
            replies.push(() => worker.reply({id: message.id, ok: true, json: {'File:FileType': 'JPEG'}}));
        };
        const first = extractBrowserExif(bytes(), 'a.jpg');
        const second = extractBrowserExif(bytes(), 'b.jpg');
        await flush();
        expect(FakeWorker.instances[0].received.map(m => m.name)).toEqual(['a.jpg']);
        replies.shift()!();
        await first;
        await flush();
        expect(FakeWorker.instances[0].received.map(m => m.name)).toEqual(['a.jpg', 'b.jpg']);
        replies.shift()!();
        expect((await second).ok).toBe(true);
    });

    test('retries a parse_error once on a fresh worker and recovers', async () => {
        const {extractBrowserExif} = await loadClient();
        FakeWorker.respond = (worker, message) => {
            if (FakeWorker.instances.length === 1) worker.reply({id: message.id, ok: false, outcome: 'parse_error'});
            else worker.reply({id: message.id, ok: true, json: {'File:FileType': 'CR3'}});
        };
        const outcome = await extractBrowserExif(bytes(), 'sample.cr3');
        expect(FakeWorker.instances).toHaveLength(2);
        expect(FakeWorker.instances[0].terminated).toBe(true);
        expect(outcome.ok).toBe(true);
    });

    test('reports parse_error when the fresh worker also fails', async () => {
        const {extractBrowserExif} = await loadClient();
        FakeWorker.respond = (worker, message) => worker.reply({id: message.id, ok: false, outcome: 'parse_error'});
        const outcome = await extractBrowserExif(bytes(), 'broken.jpg');
        expect(outcome).toMatchObject({ok: false, reason: 'parse_error'});
        expect(FakeWorker.instances).toHaveLength(2);
    });

    test('does not retry timeouts', async () => {
        const {extractBrowserExif} = await loadClient();
        FakeWorker.respond = () => {};
        const outcome = await extractBrowserExif(bytes(), 'slow.jpg', 10);
        expect(outcome).toMatchObject({ok: false, reason: 'timeout'});
        expect(FakeWorker.instances).toHaveLength(1);
    });

    test('recycles the worker after 25 parses', async () => {
        const {extractBrowserExif} = await loadClient();
        for (let i = 0; i < 26; i++) await extractBrowserExif(bytes(), `f${i}.jpg`);
        expect(FakeWorker.instances).toHaveLength(2);
        expect(FakeWorker.instances[0].terminated).toBe(true);
        expect(FakeWorker.instances[1].received).toHaveLength(1);
    });
});
