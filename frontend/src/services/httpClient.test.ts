import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AxiosError } from 'axios';

vi.mock('./authClient', () => ({ isAuthEnabled: () => false, getAccessToken: async () => null }));
vi.mock('./sessionScope', () => ({ sessionScope: () => 'same-session' }));
vi.mock('./backendStatus', () => ({ reportBackendReachable: vi.fn(), reportBackendUnreachable: vi.fn() }));
vi.mock('./sessionExpiry', () => ({ reportUnauthorized: vi.fn() }));
vi.mock('./perf', () => ({ perfEnabled: () => false, perfNow: () => 0 }));

import { createHttpClient, requestJson } from './httpClient';

beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal('localStorage', {getItem: () => null}); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each([undefined, 502, 503, 504])('retries reads after a transient %s failure', async status => {
    const client = createHttpClient('https://example.invalid');
    let attempts = 0;
    client.defaults.adapter = async config => {
        if (++attempts === 1) throw new AxiosError('unavailable', 'ERR_NETWORK', config, {}, status === undefined ? undefined : {
            data: {}, status, statusText: 'Unavailable', headers: {}, config,
        });
        return {data: {ready: true}, status: 200, statusText: 'OK', headers: {}, config};
    };
    const request = requestJson(client, 'get', '/status');
    await vi.advanceTimersByTimeAsync(1000);
    expect(await request).toEqual({ready: true});
    expect(attempts).toBe(2);
});

it.each(['post', 'put', 'delete'] as const)('does not replay a %s after losing its committed response', async method => {
    const client = createHttpClient('https://example.invalid');
    let executions = 0;
    client.defaults.adapter = async config => {
        executions++;
        throw new AxiosError('response lost after commit', 'ERR_NETWORK', config);
    };
    const request = requestJson(client, method, '/mutation', {}).catch(error => error);
    await vi.advanceTimersByTimeAsync(120_000);
    expect((await request).kind).toBe('unreachable');
    expect(executions).toBe(1);
});

it.each([502, 503, 504])('does not replay a mutation after HTTP %s', async status => {
    const client = createHttpClient('https://example.invalid');
    let attempts = 0;
    client.defaults.adapter = async config => {
        attempts++;
        throw new AxiosError('application failure', 'ERR_BAD_RESPONSE', config, {}, {
            data: {error: 'handoff failed after commit'}, status, statusText: 'Unavailable', headers: {}, config,
        });
    };
    const request = requestJson(client, 'post', '/mutation', {}).catch(error => error);
    await vi.advanceTimersByTimeAsync(120_000);
    expect((await request).status).toBe(status);
    expect(attempts).toBe(1);
});

it('honors the explicit single-attempt read option', async () => {
    const client = createHttpClient('https://example.invalid');
    const adapter = vi.fn(async config => { throw new AxiosError('offline', 'ERR_NETWORK', config); });
    client.defaults.adapter = adapter;
    await expect(requestJson(client, 'get', '/status', undefined, {singleAttempt: true})).rejects.toMatchObject({kind: 'unreachable'});
    expect(adapter).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
});
