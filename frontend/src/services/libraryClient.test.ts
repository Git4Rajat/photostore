import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ token: vi.fn(), storeToken: vi.fn(), publish: vi.fn() }));
vi.mock('./authClient', () => ({ getAccessToken: mocks.token }));
vi.mock('./passwordAuthClient', () => ({ setSessionToken: mocks.storeToken }));
vi.mock('./libraryChanges', () => ({ publishLibraryChange: mocks.publish }));
vi.mock('./apiClient', async () => {
    const { resolveServiceUrl } = await import('./apiOwnership');
    return { resolveApiUrl: (path: string, method: string) => resolveServiceUrl(path, method, 'https://core', 'https://archive') };
});
import * as library from './libraryClient';

const fetchMock = vi.fn();
beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset().mockResolvedValue({ ok: true, json: async () => ({ token: 'replacement', activeLibraryId: 'lib', status: 'queued', jobId: 'job' }) });
    mocks.token.mockReset().mockResolvedValue('session');
    mocks.storeToken.mockReset();
    mocks.publish.mockReset();
});
afterEach(() => vi.unstubAllGlobals());

it('routes all management actions to core and retains authentication headers', async () => {
    await library.getMine();
    await library.getMembers();
    await library.sendInvite('friend@example.com', 'join');
    await library.revokePendingInvite('invite');
    await library.getInviteInfo('token');
    await library.acceptInvite('token');
    await library.switchLibrary('lib');
    await library.renameLibrary('Family');
    await library.removeMember('member');
    await library.leaveLibrary('lib');
    expect(fetchMock).toHaveBeenCalledTimes(10);
    for (const [url, init] of fetchMock.mock.calls) {
        expect(url).toMatch(/^https:\/\/core\/api\/library\//);
        expect(init.headers.Authorization).toBe('Bearer session');
    }
    expect(mocks.storeToken).toHaveBeenCalledTimes(3);
    expect(mocks.storeToken).toHaveBeenLastCalledWith('replacement');
});

it('routes deletion, cleanup and both download mechanisms to archive', async () => {
    await library.deleteLibrary();
    await library.requestLibraryClean('password');
    await library.confirmLibraryClean('token');
    await library.getLibraryCleanStatus('job');
    await library.getLibraryCleanConfirmationStatus('token');
    await library.getLibraryCleanupInfo();
    await library.requestLibraryDownload();
    await library.getLibraryDownloadStatus('job');
    await library.getLibraryExportManifestPage('cursor');
    expect(fetchMock).toHaveBeenCalledTimes(9);
    for (const [url] of fetchMock.mock.calls) expect(url).toMatch(/^https:\/\/archive\/api\/library/);
    expect(fetchMock.mock.calls[0][1].method).toBe('DELETE');
    expect(mocks.publish).toHaveBeenCalledWith('library-cleanup', expect.any(Array), expect.objectContaining({ jobId: 'job', notifyCurrentTab: true }));
});

it('allows token-bound invitation info/accept without a signed-in session', async () => {
    mocks.token.mockResolvedValue(null);
    await library.getInviteInfo('a+b');
    await library.acceptInvite('token', 'password');
    expect(fetchMock.mock.calls[0][0]).toBe('https://core/api/library/invite/info?token=a%2Bb');
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined();
    expect(mocks.storeToken).toHaveBeenCalledWith('replacement');
    await expect(library.renameLibrary('Name')).rejects.toThrow('not signed in');
    expect(fetchMock).toHaveBeenCalledTimes(2);
});
