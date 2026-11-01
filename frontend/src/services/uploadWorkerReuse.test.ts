import {UploadWorkerReuse, type UploadWorker} from './uploadWorkerReuse';

const create = () => ({onmessage: null, onerror: null, postMessage: vi.fn(), terminate: vi.fn()}) as UploadWorker;
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test('bounds idle retention, expires unused workers and cancels timers when disposed', async () => {
    const pool = new UploadWorkerReuse(2, 100);
    const workers = [create(), create(), create()];
    for (const worker of workers) pool.release(worker);
    expect(workers[0].terminate).toHaveBeenCalledOnce();
    const active = pool.take(create);
    expect(active).toBe(workers[2]);
    await vi.advanceTimersByTimeAsync(100);
    expect(workers[1].terminate).toHaveBeenCalledOnce();
    expect(active.terminate).not.toHaveBeenCalled();
    pool.dispose();
    pool.release(active);
    expect(active.terminate).toHaveBeenCalledOnce();
    expect(() => pool.take(create)).toThrow('disposed');
    expect(vi.getTimerCount()).toBe(0);
});

test('evicts an idle worker that reports an error before it can be reused', () => {
    const pool = new UploadWorkerReuse();
    const failed = create();
    pool.release(failed);
    failed.onerror?.call(failed as Worker, new ErrorEvent('error'));
    expect(failed.terminate).toHaveBeenCalledOnce();
    const replacement = pool.take(create);
    expect(replacement).not.toBe(failed);
    pool.release(replacement);
    pool.dispose();
    expect(vi.getTimerCount()).toBe(0);
});
