// @vitest-environment node
import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ post: vi.fn(), get: vi.fn(), scope: 'test' }));
vi.mock('./sessionScope', () => ({ sessionScope: () => mocks.scope }));
vi.mock('./apiClient', () => ({ postUpload: mocks.post, getUpload: mocks.get }));
import { renewUploadWaveLease, waveLiveEnabled, waveProtocol, closeWaveShadowSession, openWaveShadowSession, registerWaveShadowItems, resolveWaveShadowItem, resolveWaveShadowItems } from './uploadWaveClient';

beforeEach(() => {
    mocks.scope = crypto.randomUUID();
    mocks.get.mockReset().mockResolvedValue({ protocol: 'live' });
    vi.stubGlobal('crypto', webcrypto);
    vi.stubEnv('VITE_UPLOAD_WAVE_SHADOW_ENABLED', 'true');
    mocks.post.mockReset().mockResolvedValue({ manifestRevision: 'rev-1' });
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe('shadow wire protocol', () => {
    it('hashes batched outcomes consistently with manifest registration', async () => {
        await registerWaveShadowItems('parent', [{ itemId: 'key:a', filename: 'a.jpg' }]);
        await resolveWaveShadowItems('parent', [{ itemId: 'key:a', outcome: 'duplicate', filename: 'a.jpg' },
            { itemId: 'key:b', outcome: 'failed' }]);
        const [url, body, config] = mocks.post.mock.calls[1];
        expect(url).toContain('/resolve-batch');
        expect(body.items[0]).toEqual({ itemId: mocks.post.mock.calls[0][1].items[0].itemId,
            outcome: 'duplicate', filename: 'a.jpg' });
        expect(body.items[1].itemId).toMatch(/^[a-f0-9]{64}$/);
        expect(config).toEqual({ singleAttempt: true, timeout: 120000 });
    });
    it('makes no requests while client diagnostics are disabled', async () => {
        vi.stubEnv('VITE_UPLOAD_WAVE_SHADOW_ENABLED', 'false');
        expect(await openWaveShadowSession('parent', 1)).toBeNull();
        expect(await registerWaveShadowItems('parent', [{ itemId: 'file:key', filename: 'a.jpg' }])).toBeNull();
        expect(await resolveWaveShadowItem('parent', 'file:key', 'accepted', 'a.jpg')).toBeNull();
        expect(await closeWaveShadowSession('parent', 'rev-1', 'close-1')).toBeNull();
        expect(mocks.post).not.toHaveBeenCalled();
    });

    it('uses stable backend-compatible IDs for actual persisted keys, including long Unicode filenames', async () => {
        const key = `1700000000000-abcd1234:0:${'假期：照片'.repeat(50)}.jpg:123456:1700000000000`;
        const item = { itemId: key, filename: '假期照片.jpg' };
        await registerWaveShadowItems('parent', [item]);
        const registered = mocks.post.mock.calls[0][1].items[0];
        expect(registered.itemId).toMatch(/^[a-f0-9]{64}$/);
        expect(registered.filename).toBe(item.filename);
        expect(item.itemId).toBe(key);
        await resolveWaveShadowItem('parent', key, 'accepted', item.filename);
        expect(mocks.post.mock.calls[1][1].itemId).toBe(registered.itemId);
        await registerWaveShadowItems('parent', [item, { itemId: key + ':different', filename: item.filename }]);
        const replay = mocks.post.mock.calls[2][1].items;
        expect(replay[0].itemId).toBe(registered.itemId);
        expect(replay[1].itemId).not.toBe(registered.itemId);
    });

    it('does not reject the upload caller when hashing or transport fails', async () => {
        vi.stubGlobal('crypto', {});
        expect(await resolveWaveShadowItem('parent', 'key', 'failed')).toBeNull();
        expect(mocks.post).not.toHaveBeenCalled();
        mocks.post.mockRejectedValue(new Error('offline'));
        expect(await openWaveShadowSession('parent', 1)).toBeNull();
    });
});

it('discovers live ownership per library and preserves legacy uploads in the same build', async () => {
    vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
    mocks.get.mockResolvedValueOnce({ protocol: 'legacy' });
    expect(await waveProtocol()).toBe('legacy');
    expect(await openWaveShadowSession('old', 1)).toBeNull();
    expect(mocks.post).not.toHaveBeenCalled();
    mocks.scope = 'another-library';
    expect(await openWaveShadowSession('new', 1)).not.toBeNull();
    expect(mocks.post.mock.calls[0][0]).toContain('/waves/live/');
    expect(mocks.get).toHaveBeenCalledTimes(2);
});
it('never guesses legacy ownership after a discovery error and permits retry', async () => {
    mocks.get.mockRejectedValueOnce(new Error('storage unavailable'));
    await expect(waveProtocol()).rejects.toThrow('storage unavailable');
    await expect(waveProtocol()).resolves.toBe('live');
});

it('preserves live admission errors while outcome reports remain best effort', async () => {
    vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
    mocks.post.mockRejectedValue({ response: { data: { error: 'Library cleanup is in progress' } } });
    await expect(openWaveShadowSession('new-selection', 3544)).rejects.toThrow('Library cleanup is in progress');
    await expect(registerWaveShadowItems('new-selection', [{ itemId: 'key', filename: '000201.webp' }]))
        .rejects.toThrow('Library cleanup is in progress');
    await expect(resolveWaveShadowItem('stopped-selection', 'key', 'cancelled')).resolves.toBeNull();
});

it('allows the deployed runtime flag to opt in without a special frontend image build', async () => {
    vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'false');
    vi.stubGlobal('window', { __APP_CONFIG__: { uploadWaveExecutionEnabled: true } });
    expect(waveLiveEnabled()).toBe(true);
    await openWaveShadowSession('runtime-parent', 1);
    expect(mocks.post.mock.calls[0][0]).toContain('/waves/live/');
    window.__APP_CONFIG__!.uploadWaveExecutionEnabled = false;
    expect(waveLiveEnabled()).toBe(false);
});


it('retries only transient 409 contention with the identical admission payload', async () => {
    vi.stubGlobal('crypto', { subtle: { digest: async () => new Uint8Array(32).buffer } });
    vi.useFakeTimers();
    try {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        mocks.post.mockRejectedValueOnce({response: {status: 409, data: {code: 'wave_mutation_contention', error: 'Concurrent wave mutation; retry the request'}}})
            .mockResolvedValueOnce({manifestRevision: 'registered', registeredCount: 1});
        const pending = registerWaveShadowItems('retry', [{itemId: 'stable', filename: 'photo.jpg'}]);
        await vi.runAllTimersAsync();
        await expect(pending).resolves.toEqual({manifestRevision: 'registered', registeredCount: 1});
        expect(mocks.post).toHaveBeenCalledTimes(2);
        expect(mocks.post.mock.calls[0][1]).toEqual(mocks.post.mock.calls[1][1]);
    } finally { vi.useRealTimers(); }
});

it('does not retry a permanent admission conflict', async () => {
    vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
    mocks.post.mockRejectedValue({response: {status: 409, data: {error: 'Session is closed'}}});
    await expect(openWaveShadowSession('closed', 1)).rejects.toThrow('Session is closed');
    expect(mocks.post).toHaveBeenCalledTimes(1);
});

it('bounds contention retries and stops replay if the authenticated scope changes', async () => {
    vi.useFakeTimers();
    try {
        vi.stubEnv('VITE_UPLOAD_WAVE_EXECUTION_ENABLED', 'true');
        const error = {response: {status: 409, data: {code: 'wave_mutation_contention', error: 'Concurrent wave mutation; retry the request'}}};
        mocks.post.mockRejectedValue(error);
        const pending = openWaveShadowSession('busy', 1);
        const checked = expect(pending).rejects.toThrow('Concurrent wave mutation');
        await vi.runAllTimersAsync(); await checked;
        expect(mocks.post).toHaveBeenCalledTimes(4);
        mocks.post.mockClear();
        mocks.post.mockImplementationOnce(async () => { mocks.scope = 'other-account'; throw error; });
        const changed = openWaveShadowSession('busy', 1);
        await vi.runAllTimersAsync();
        expect(await changed).toBeNull();
        expect(mocks.post).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
});


it('renews the session lease using the existing heartbeat transport', async () => {
    mocks.post.mockResolvedValue({leaseActive:false,leaseExpiresAt:123,serverNow:456});
    expect(await renewUploadWaveLease('parent')).toMatchObject({leaseActive:false});
    expect(mocks.post).toHaveBeenCalledWith('/upload/transfer-heartbeat',
        {waveSessionId:'parent'},{singleAttempt:true,timeout:15000});
});

it('rejects heartbeat acknowledgment after switching libraries', async () => {
    mocks.post.mockImplementationOnce(async () => { mocks.scope='other';return {leaseActive:true}; });
    await expect(renewUploadWaveLease('parent')).rejects.toThrow('Upload library changed');
});
