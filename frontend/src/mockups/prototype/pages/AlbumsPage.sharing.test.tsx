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
const stopsOn = (iso: string) => `Stops working on ${new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(iso))}`;
const expiryButton = (label: string) => screen.getByRole('button', { name: label });
const activeExpiry = () => document.querySelector('.pt-share-expiry button.active')?.textContent ?? null;
const codeSwitch = () => screen.getByRole('switch', { name: 'Ask for a code' });

// Opens the album and its share sheet.
const renderAlbum = async (isPublic: boolean, extra: { hasAccessCode?: boolean; publicExpiresAt?: string } = {}) => {
    mocks.albumsIndex.mockResolvedValue([{ albumId: 'a1', name: 'Trip', photoCount: 0, coverFilename: '', isPublic, publicUrl: isPublic ? sharedAlbum.publicUrl : '', ...extra }]);
    render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
    await act(async () => { store.navigate('albums', { albumId: 'a1' }); });
    await screen.findByRole('heading', { name: 'Trip' });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Share' })); });
    await screen.findByRole('dialog', { name: 'Share “Trip”' });
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

    // The sheet is the deliberate step: it says what viewers see before the switch.
    it('turning the link on says what viewers can see and makes a 7-day link', async () => {
        await renderAlbum(false);
        expect(screen.getByText(/nothing else in your library/)).toBeTruthy();
        expect(shareCalls()).toHaveLength(0);
        await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'Link sharing' })); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(shareCalls()[0][1]).toMatchObject({ enabled: true, expiresInDays: 7 });
    });

    it('stop sharing says who loses access, then revokes the link', async () => {
        await renderAlbum(true);
        expect(screen.getByText('Anyone with the link loses access straight away.')).toBeTruthy();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop sharing' })); });
        await waitFor(() => expect(revokeCalls()).toHaveLength(1));
    });

    it('shows a new access code inline and never in a toast', async () => {
        await renderAlbum(true);
        await act(async () => { fireEvent.click(codeSwitch()); });
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
        await act(async () => { fireEvent.click(codeSwitch()); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(document.querySelector('.pt-access-code')).toBeNull();
    });

    // A protected album must not become reachable with the link alone
    // because the owner picked a different expiry.
    it('changing the link expiry keeps an existing access code', async () => {
        await renderAlbum(true, { hasAccessCode: true });
        await act(async () => { fireEvent.click(expiryButton('30 days')); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(shareCalls()[0][1]).toMatchObject({ expiresInDays: 30, keepExpiry: false, accessCode: '', clearAccessCode: false });
        expect(codeSwitch().getAttribute('aria-checked')).toBe('true');
    });

    it('keeps a code generated this session when the expiry changes afterwards', async () => {
        await renderAlbum(true);
        await act(async () => { fireEvent.click(codeSwitch()); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        const code = String((shareCalls()[0][1] as { accessCode: string }).accessCode);
        await screen.findByText(code);

        await act(async () => { fireEvent.click(expiryButton('1 day')); });
        await waitFor(() => expect(shareCalls()).toHaveLength(2));
        expect(shareCalls()[1][1]).toMatchObject({ expiresInDays: 1, clearAccessCode: false });
        expect(screen.getByText(code).className).toContain('pt-access-code');
    });

    // The expiry used to start at "7 days" whatever the link was set to, and
    // the code actions re-sent that, shortening a "Never" link to a week.
    it('shows the link’s own expiry', async () => {
        const expiresAt = inDays(29.5);
        await renderAlbum(true, { publicExpiresAt: expiresAt });
        expect(screen.getByText(stopsOn(expiresAt))).toBeTruthy();
        expect(activeExpiry()).toBe('30 days');
        cleanup();

        await renderAlbum(true);
        expect(screen.getByText('Never stops working')).toBeTruthy();
        expect(activeExpiry()).toBe('Never');
    });

    it('a new code leaves a never-expiring link without an expiry', async () => {
        await renderAlbum(true);
        await act(async () => { fireEvent.click(codeSwitch()); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        // An older server ignores keepExpiry and applies expiresInDays: 0 = never.
        expect(shareCalls()[0][1]).toMatchObject({ keepExpiry: true, expiresInDays: 0 });
        await waitFor(() => expect(screen.getByText('Never stops working')).toBeTruthy());
    });

    it('a new code keeps a dated link’s expiry', async () => {
        const expiresAt = inDays(2.5);
        await renderAlbum(true, { publicExpiresAt: expiresAt });
        await act(async () => { fireEvent.click(codeSwitch()); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        // Rounded up, so an older server never cuts the link short.
        expect(shareCalls()[0][1]).toMatchObject({ keepExpiry: true, expiresInDays: 3 });
        expect(screen.getByText(stopsOn(expiresAt))).toBeTruthy();
    });

    it('shows the new date once the server confirms an expiry change', async () => {
        await renderAlbum(true);
        let respond: (value: unknown) => void = () => {};
        mocks.post.mockImplementationOnce(() => new Promise((resolve) => { respond = resolve; }));
        await act(async () => { fireEvent.click(expiryButton('30 days')); });
        expect(activeExpiry()).toBe('30 days');

        const expiresAt = inDays(30);
        await act(async () => { respond({ album: { ...sharedAlbum, publicExpiresAt: expiresAt } }); });
        expect(screen.getByText(stopsOn(expiresAt))).toBeTruthy();
    });

    it('puts the expiry back when a change fails', async () => {
        await renderAlbum(true);
        mocks.post.mockRejectedValue(new Error('offline'));
        await act(async () => { fireEvent.click(expiryButton('30 days')); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        await waitFor(() => expect(activeExpiry()).toBe('Never'));
    });

    it('removes an access code only after the owner confirms', async () => {
        await renderAlbum(true, { hasAccessCode: true });
        mocks.confirm.mockResolvedValue(false);
        await act(async () => { fireEvent.click(codeSwitch()); });
        expect(mocks.confirm).toHaveBeenCalledWith(expect.objectContaining({
            title: 'Remove the access code from “Trip”?',
            danger: true,
        }));
        expect(shareCalls()).toHaveLength(0);

        mocks.confirm.mockResolvedValue(true);
        await act(async () => { fireEvent.click(codeSwitch()); });
        await waitFor(() => expect(shareCalls()).toHaveLength(1));
        expect(shareCalls()[0][1]).toMatchObject({ accessCode: '', clearAccessCode: true, keepExpiry: true });
    });

    it('offers no new-code action on an album without a code', async () => {
        await renderAlbum(true);
        expect(codeSwitch().getAttribute('aria-checked')).toBe('false');
        expect(screen.queryByRole('button', { name: /Make a new code/ })).toBeNull();
    });
});

describe('what reaches the bell', () => {
    beforeEach(() => {
        mocks.get.mockReset().mockResolvedValue({});
        mocks.post.mockReset().mockResolvedValue({});
        mocks.albumsIndex.mockReset().mockResolvedValue([]);
        mocks.notify.mockReset().mockReturnValue(true);
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    // Quick confirmations show for a moment and aren't saved; problems are.
    it('keeps confirmations out of the saved notifications, but not problems', async () => {
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async () => { store.toast('Album renamed'); });
        expect(mocks.notify).not.toHaveBeenCalled();
        expect(store.toasts.map((t) => t.message)).toContain('Album renamed');

        await act(async () => { store.toast('Couldn’t rename the album', undefined, undefined, 'error'); });
        expect(mocks.notify).toHaveBeenCalledWith('Couldn’t rename the album', '', undefined);
    });
});
