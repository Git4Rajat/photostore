// @vitest-environment node
import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ post: vi.fn() }));
vi.mock('./apiClient', () => ({ postUpload: mocks.post }));
import { closeWaveShadowSession, openWaveShadowSession, registerWaveShadowItems, resolveWaveShadowItem } from './uploadWaveClient';

beforeEach(() => {
    vi.stubGlobal('crypto', webcrypto);
    vi.stubEnv('VITE_UPLOAD_WAVE_SHADOW_ENABLED', 'true');
    mocks.post.mockReset().mockResolvedValue({ manifestRevision: 'rev-1' });
});
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe('shadow wire protocol', () => {
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
