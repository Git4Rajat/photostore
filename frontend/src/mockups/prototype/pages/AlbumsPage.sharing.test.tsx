import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), albumsIndex: vi.fn(), confirm: vi.fn(), notify: vi.fn() }));
vi.mock('../../../services/apiClient', () => ({ get: mocks.get, post: mocks.post, postTools: vi.fn() }));
vi.mock('../../../services/localSortIndex', () => ({
    getLocalSortIndex: vi.fn().mockResolvedValue([]), invalidateLocalSortIndex: vi.fn(), isServerPagedLibrary: () => false,
    patchLocalSortIndexRow: vi.fn(), removeLocalSortIndexRows: vi.fn(), clearLocalSortIndexRemovals: vi.fn(),
}));
vi.mock('../../../services/mediaToken', () => ({
    getMediaToken: vi.fn().mockResolvedValue(null), getCachedMediaToken: () => null, thumbnailUrlForBlob: () => '',
}));
vi.mock('../../../services/backgroundRequestQueue', () => ({ enqueueBackgroundRequest: (task: () => Promise<unknown>) => task() }));
vi.mock('../../../services/indexBuilding', () => ({ reportIndexBuilding: vi.fn(), onIndexReady: () => () => {} }));
vi.mock('../../../services/faceService', () => ({ default: {} }));
vi.mock('../../../services/libraryClient', () => ({}));
vi.mock('../../../services/thumbnailAccessCache', () => ({ resolveThumbnailAccessUrls: vi.fn().mockResolvedValue(new Map()) }));
vi.mock('../../../services/localAlbumsIndex', () => ({ getLocalAlbumsIndex: mocks.albumsIndex, invalidateLocalAlbumsIndex: vi.fn() }));
vi.mock('../../../services/localPeopleIndex', () => ({ invalidateLocalPeopleIndex: vi.fn() }));
vi.mock('../../../services/jobNotifications', () => ({ requestJobPoll: vi.fn() }));
vi.mock('../../../services/imageClient', () => ({ useProtectedBlobUrls: () => ({}) }));
// Whatever reaches the sink lands in the saved notification history.
vi.mock('../../../services/notificationSink', () => ({ publishNotification: mocks.notify }));
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: mocks.confirm, promptDialog: vi.fn() }));

import { AlbumsPage } from './AlbumsPage';
import { StoreProvider, useStore } from '../store';

let store: ReturnType<typeof useStore>;
function Probe() {
    store = useStore();
    return null;
}

const sharedAlbum = { id: 'a1', name: 'Trip', photoCount: 0, isPublic: true, publicUrl: 'https://keepsake.invalid/public/album/t1' };
const shareCalls = () => mocks.post.mock.calls.filter(([path]) => path === '/albums/a1/share');
const revokeCalls = () => mocks.post.mock.calls.filter(([path]) => path === '/albums/a1/revoke');
const inDays = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString();
const onDate = (iso: string) => `On ${new Date(iso).toLocaleDateString()}`;

const renderAlbum = async (isPublic: boolean, extra: { hasAccessCode?: boolean; publicExpiresAt?: string } = {}) => {
    mocks.albumsIndex.mockResolvedValue([{ albumId: 'a1', name: 'Trip', photoCount: 0, coverFilename: '', isPublic, publicUrl: isPublic ? sharedAlbum.publicUrl : '', ...extra }]);
    render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
    // The bare Albums tab: the first album shows without ?albumId.
    await act(async () => { store.navigate('albums', {}); });
    await screen.findByRole('heading', { name: 'Trip' });
};

describe('album sharing', () => {
    beforeEach(() => {
        mocks.get.mockReset().mockResolvedValue({});
        // Answers like the server: keepExpiry leaves the expiry alone, otherwise
        // expiresInDays sets it (0 = never).
        mocks.post.mockReset().mockImplementation(async (path: string, body: { expiresInDays?: number; keepExpiry?: boolean }) => (
            path === '/albums/a1/share'
                ? { album: { ...sharedAlbum, ...(body.keepExpiry ? {} : { publicExpiresAt: body.expiresInDays ? inDays(body.expiresInDays) : '' }) } }
                : { album: { ...sharedAlbum, isPublic: false, publicUrl: undefined } }
        ));
        mocks.albumsIndex.mockReset();
        mocks.confirm.mockReset();
        mocks.notify.mockReset().mockReturnValue(false);
        vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    it('turning sharing on is an explicit choice', async () => {
        await renderAlbum(false);
        mocks.confirm.mockResolvedValue(false);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Share' })); });
        expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({
            title: 'Share “Trip”?',
            message: expect.stringContaining('The link expires in 7 days.'),
        }));
        expect(shareCalls()).toHaveLength(0);

        mocks.confirm.mockResolvedValue(true);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Share' })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(shareCalls()[0][1]).toMatchObject({ enabled: true, expiresInDays: 7 });
    });

    it('asks before stopping sharing, since the link stops working at once', async () => {
        await renderAlbum(true);
        mocks.confirm.mockResolvedValue(false);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sharing on' })); });
        expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({
            message: 'Anyone with the link loses access straight away.',
            danger: true,
        }));
        expect(revokeCalls()).toHaveLength(0);

        mocks.confirm.mockResolvedValue(true);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sharing on' })); });
        await waitFor(() => expect(revokeCalls()).toHaveLength(1));
    });

    it('shows a new access code inline and never in a toast', async () => {
        await renderAlbum(true);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New code/ })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        const code = String((shareCalls()[0][1] as { accessCode: string }).accessCode);
        expect(code).toMatch(/^[0-9A-F]{8}$/);

        expect((await screen.findByText(code)).className).toContain('pt-access-code');
        const toasted = [...mocks.notify.mock.calls.map(([title]) => String(title)), ...store.toasts.map((t) => t.message)];
        expect(toasted.some((text) => text.includes(code))).toBe(false);
    });

    it('does not show a code the server never accepted', async () => {
        await renderAlbum(true);
        mocks.post.mockRejectedValue(new Error('offline'));
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New code/ })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(document.querySelector('.pt-access-code')).toBeNull();
    });

    // A protected album must not become reachable with the link alone
    // because the owner picked a different expiry.
    it('changing the link expiry keeps an existing access code', async () => {
        await renderAlbum(true, { hasAccessCode: true });
        await act(async () => { fireEvent.change(screen.getByDisplayValue('Never'), { target: { value: '30' } }); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(shareCalls()[0][1]).toMatchObject({ expiresInDays: 30, keepExpiry: false, accessCode: '', clearAccessCode: false });
        expect(screen.getByText('Protected — generate a new code to reveal one')).toBeTruthy();
    });

    it('keeps a code generated this session when the expiry changes afterwards', async () => {
        await renderAlbum(true);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New code/ })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        const code = String((shareCalls()[0][1] as { accessCode: string }).accessCode);
        await screen.findByText(code);

        await act(async () => { fireEvent.change(screen.getByDisplayValue('Never'), { target: { value: '1' } }); });
        await waitFor(() => expect(shareCalls()).toHaveLength(2));
        expect(shareCalls()[1][1]).toMatchObject({ expiresInDays: 1, clearAccessCode: false });
        expect(screen.getByText(code).className).toContain('pt-access-code');
    });

    // The dropdown used to start at "In 7 days" whatever the link was set to,
    // and the code actions re-sent that, shortening a "Never" link to a week.
    it('shows the link’s own expiry in the dropdown', async () => {
        const expiresAt = inDays(12);
        await renderAlbum(true, { publicExpiresAt: expiresAt });
        expect(screen.getByDisplayValue(onDate(expiresAt))).toBeTruthy();
        cleanup();

        await renderAlbum(true);
        expect(screen.getByDisplayValue('Never')).toBeTruthy();
    });

    it('a new code leaves a never-expiring link without an expiry', async () => {
        await renderAlbum(true);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New code/ })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        // An older server ignores keepExpiry and applies expiresInDays: 0 = never.
        expect(shareCalls()[0][1]).toMatchObject({ keepExpiry: true, expiresInDays: 0 });
        await waitFor(() => expect(screen.getByDisplayValue('Never')).toBeTruthy());
    });

    it('a new code keeps a dated link’s expiry', async () => {
        const expiresAt = inDays(2.5);
        await renderAlbum(true, { publicExpiresAt: expiresAt });
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: /New code/ })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        // Rounded up, so an older server never cuts the link short.
        expect(shareCalls()[0][1]).toMatchObject({ keepExpiry: true, expiresInDays: 3 });
        expect(screen.getByDisplayValue(onDate(expiresAt))).toBeTruthy();
    });

    it('shows the new date once the server confirms an expiry change', async () => {
        await renderAlbum(true);
        let respond: (value: unknown) => void = () => {};
        mocks.post.mockImplementationOnce(() => new Promise((resolve) => { respond = resolve; }));
        await act(async () => { fireEvent.change(screen.getByDisplayValue('Never'), { target: { value: '30' } }); });
        expect(screen.getByDisplayValue('In 30 days')).toBeTruthy();

        const expiresAt = inDays(30);
        await act(async () => { respond({ album: { ...sharedAlbum, publicExpiresAt: expiresAt } }); });
        expect(screen.getByDisplayValue(onDate(expiresAt))).toBeTruthy();
    });

    it('puts the dropdown back when an expiry change fails', async () => {
        await renderAlbum(true);
        mocks.post.mockRejectedValue(new Error('offline'));
        await act(async () => { fireEvent.change(screen.getByDisplayValue('Never'), { target: { value: '30' } }); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        await waitFor(() => expect(screen.getByDisplayValue('Never')).toBeTruthy());
    });

    it('removes an access code only after the owner confirms', async () => {
        await renderAlbum(true, { hasAccessCode: true });
        mocks.confirm.mockResolvedValue(false);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Remove code' })); });
        expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({
            title: 'Remove the access code from “Trip”?',
            danger: true,
        }));
        expect(shareCalls()).toHaveLength(0);

        mocks.confirm.mockResolvedValue(true);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Remove code' })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(shareCalls()[0][1]).toMatchObject({ accessCode: '', clearAccessCode: true, keepExpiry: true });
    });

    it('offers no Remove code action on an album without a code', async () => {
        await renderAlbum(true);
        expect(screen.queryByRole('button', { name: 'Remove code' })).toBeNull();
    });
});
