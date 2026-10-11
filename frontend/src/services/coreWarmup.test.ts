import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import source from '../../public/core-warmup.js?raw';

const run = (config: Record<string, unknown>, fetcher: typeof fetch, online = true) => {
    const scope = { __APP_CONFIG__: config };
    new Function('window', 'navigator', 'fetch', 'AbortController', 'setTimeout', 'clearTimeout', source)(
        scope, { onLine: online }, fetcher, AbortController, setTimeout, clearTimeout,
    );
    return scope;
};

describe('early core wake-up', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('starts immediately with no authentication and stops after a response', async () => {
        const fetcher = vi.fn().mockResolvedValue(new Response('ok'));
        run({ apiBaseUrl: 'https://core.invalid/' }, fetcher);
        expect(fetcher).toHaveBeenCalledWith('https://core.invalid/health', expect.objectContaining({ credentials: 'omit', cache: 'no-store' }));
        expect(fetcher.mock.calls[0][1]).not.toHaveProperty('headers');
        await vi.runAllTimersAsync();
        expect(fetcher).toHaveBeenCalledTimes(1);
    });

    it('retries transient ingress failures with a bounded attempt count', async () => {
        const fetcher = vi.fn().mockResolvedValue(new Response('', { status: 503 }));
        run({ apiBaseUrl: 'https://core.invalid' }, fetcher);
        await vi.runAllTimersAsync();
        expect(fetcher).toHaveBeenCalledTimes(6);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('aborts stalled attempts and retries without waiting indefinitely', async () => {
        const fetcher = vi.fn((_url, options) => new Promise<Response>((_resolve, reject) => {
            options.signal.addEventListener('abort', () => reject(new Error('aborted')));
        }));
        run({ apiBaseUrl: 'https://core.invalid' }, fetcher as typeof fetch);
        await vi.runAllTimersAsync();
        expect(fetcher).toHaveBeenCalledTimes(6);
        expect(vi.getTimerCount()).toBe(0);
    });

    it('does not send requests without configuration or while offline', () => {
        const fetcher = vi.fn();
        run({}, fetcher);
        run({ apiBaseUrl: 'https://core.invalid' }, fetcher, false);
        expect(fetcher).not.toHaveBeenCalled();
    });
});
