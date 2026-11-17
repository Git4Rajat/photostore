import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
vi.mock('../../../services/notificationSink', () => ({ publishNotification: mocks.notify }));
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: mocks.confirm, promptDialog: vi.fn() }));

import { AlbumsPage } from './AlbumsPage';
import { StoreProvider, useStore } from '../store';
import { AppSidebar } from '../components/AppSidebar';

let store: ReturnType<typeof useStore>;
function Probe() {
    store = useStore();
    return null;
}

const albumRows = (n: number) => Array.from({ length: n }, (_, i) => ({ albumId: `a${i}`, name: `Album ${i}`, photoCount: 1, coverFilename: '' }));

describe('albums page ways in', () => {
    beforeEach(() => {
        mocks.get.mockReset().mockResolvedValue({});
        mocks.post.mockReset().mockResolvedValue({});
        mocks.albumsIndex.mockReset();
        vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    });
    afterEach(() => { cleanup(); vi.restoreAllMocks(); });

    // Deleting the last album used to drop People and Places from the page with it.
    it('keeps People and places when there are no albums', async () => {
        mocks.albumsIndex.mockResolvedValue([]);
        render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
        await act(async () => { store.navigate('albums'); await store.fetchAlbums(true); });
        expect(await screen.findByText('No albums yet.')).toBeTruthy();
        expect(screen.getByRole('heading', { name: 'People & places' })).toBeTruthy();
        const links = screen.getByRole('navigation', { name: 'People and places' });
        expect(within(links).getByRole('button', { name: /People/ })).toBeTruthy();
        expect(within(links).getByRole('button', { name: /Places/ })).toBeTruthy();
    });

    // The mock-up has People and Places in one list and no media-types group.
    it('lists People and Places once, with no media types', async () => {
        mocks.albumsIndex.mockResolvedValue(albumRows(3));
        render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
        await act(async () => { store.navigate('albums'); await store.fetchAlbums(true); });
        expect(await screen.findByRole('heading', { name: 'People & places' })).toBeTruthy();
        expect(screen.getAllByRole('navigation', { name: 'People and places' }).length).toBe(1);
        expect(screen.queryByText('Media types')).toBeNull();
        expect(screen.queryByRole('button', { name: /Videos/ })).toBeNull();
    });

    // A button after everything else sat under the tab bar on phones; smart albums are a labelled suggestion
    // right below People and places, as in the mock-up.
    it('offers smart albums as a suggestion under People and places', async () => {
        mocks.albumsIndex.mockResolvedValue(albumRows(3));
        render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
        await act(async () => { store.navigate('albums'); await store.fetchAlbums(true); });
        const suggested = await screen.findByRole('heading', { name: 'Suggested' });
        const places = screen.getByRole('heading', { name: 'People & places' });
        expect(places.compareDocumentPosition(suggested) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
        expect(screen.getByText('Made only if you tap Create')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Create a smart album' }));
        expect(await screen.findByText('New smart album')).toBeTruthy();
        expect(screen.getByRole('button', { name: /By location/ })).toBeTruthy();
    });

    // The sidebar used to stop at eight albums with no way to reach the rest.
    it('lists every album in the sidebar, in its own scrolling list', async () => {
        mocks.albumsIndex.mockResolvedValue(albumRows(12));
        render(<StoreProvider><Probe /><AppSidebar /></StoreProvider>);
        await waitFor(() => expect(document.querySelectorAll('.pt-side-albums button').length).toBe(12));
        const names = Array.from(document.querySelectorAll('.pt-side-albums button span:not(.pt-side-thumb)'), (n) => n.textContent);
        expect(names).toEqual(albumRows(12).map((a) => a.name));
        expect(screen.getByRole('button', { name: /Recently deleted/ }).closest('.pt-side-albums')).toBeNull();
    });
});

describe('selecting albums by long press', () => {
    // The name and the count are separate inline elements, so their text runs together ("Album 0" + "1 item").
    // (The list view's cover cards; the CSS-hidden desktop list has rows of the same name.)
    const cards = () => within(document.querySelector('.pt-album-cards') as HTMLElement);
    const card = (n: number) => cards().getByRole('button', { name: `Album ${n}1 item` });
    const pressed = (n: number) => card(n).getAttribute('aria-pressed');
    // Press, hold, release; the browser then sends the click that ends every touch.
    const hold = async (el: Element, pointerType: string) => {
        fireEvent.pointerDown(el, { pointerType, clientX: 10, clientY: 10 });
        await act(async () => { vi.advanceTimersByTime(600); });
        fireEvent.pointerUp(el, { pointerType, clientX: 10, clientY: 10 });
        fireEvent.click(el);
    };

    beforeEach(async () => {
        mocks.get.mockReset().mockResolvedValue({});
        mocks.post.mockReset().mockResolvedValue({});
        mocks.albumsIndex.mockReset().mockResolvedValue(albumRows(3));
        vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
        render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
        await act(async () => { store.navigate('albums'); await store.fetchAlbums(true); });
        await waitFor(() => expect(document.querySelectorAll('.pt-album-cards button').length).toBe(3));
        vi.useFakeTimers();
    });
    afterEach(() => { vi.useRealTimers(); cleanup(); vi.restoreAllMocks(); });

    it('starts selecting with the held album picked, without opening it', async () => {
        await hold(card(1), 'touch');
        expect(pressed(1)).toBe('true');
        expect(pressed(0)).toBe('false');
        expect(screen.getByRole('toolbar', { name: 'Albums selection actions' }).textContent).toContain('1');
        // The click that ended the hold neither opened the album nor undid the pick.
        expect(pressed(1)).toBe('true');
        expect(store.route.params.albumId).toBeUndefined();
    });

    it('keeps adding albums by tap or by another hold once selecting', async () => {
        await hold(card(0), 'touch');
        fireEvent.click(card(2));
        expect(pressed(2)).toBe('true');
        await hold(card(1), 'touch');
        expect([pressed(0), pressed(1), pressed(2)]).toEqual(['true', 'true', 'true']);
        await hold(card(1), 'touch');
        expect(pressed(1)).toBe('false');
        fireEvent.click(card(0));
        expect(pressed(0)).toBe('false');
    });

    it('opens the album on a plain tap, and ignores a hold with a mouse', async () => {
        await hold(card(2), 'mouse');
        expect(card(2).getAttribute('aria-pressed')).toBeNull();
        expect(screen.queryByRole('toolbar', { name: 'Albums selection actions' })).toBeNull();
        fireEvent.pointerDown(card(2), { pointerType: 'touch', clientX: 10, clientY: 10 });
        await act(async () => { vi.advanceTimersByTime(100); });
        fireEvent.pointerUp(card(2), { pointerType: 'touch', clientX: 10, clientY: 10 });
        fireEvent.click(card(2));
        expect(store.route.params.albumId).toBe('a2');
    });

    it('does not start when the finger moves, as in a scroll', async () => {
        fireEvent.pointerDown(card(0), { pointerType: 'touch', clientX: 10, clientY: 10 });
        fireEvent.pointerMove(card(0), { pointerType: 'touch', clientX: 10, clientY: 60 });
        await act(async () => { vi.advanceTimersByTime(600); });
        expect(card(0).getAttribute('aria-pressed')).toBeNull();
    });
});
