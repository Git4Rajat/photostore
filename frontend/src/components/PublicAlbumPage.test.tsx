import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../services/apiClient', () => ({
    getExtras: mocks.get, postExtras: mocks.post, resolveApiUrl: (path: string) => path,
}));
vi.mock('../services/useBackendRecoveryRetry', () => ({ useBackendRecoveryRetry: () => {} }));
vi.mock('../services/requestFeedback', () => ({ notifyApiError: vi.fn() }));

import PublicAlbumPage from './PublicAlbumPage';
import { ApiError } from '../services/apiError';

// What requestJson throws: the server's JSON survives on responseData.
const apiError = (status: number, responseData: unknown) => new ApiError({
    kind: 'client', message: 'Request failed', rawMessage: 'Request failed', status, retriable: false, requestId: 't', responseData,
});

const renderPage = () => render(
    <MemoryRouter initialEntries={['/public/album/tok']}>
        <Routes><Route path="/public/album/:token" element={<PublicAlbumPage />} /></Routes>
    </MemoryRouter>,
);

describe('shared album page', () => {
    beforeEach(() => { mocks.get.mockReset(); mocks.post.mockReset(); });
    afterEach(cleanup);

    it.each(['Album expired', 'Album not public', 'Album not found'])('says plainly that the link no longer works (%s)', async (error) => {
        mocks.get.mockRejectedValue(apiError(404, { error }));
        renderPage();
        expect(await screen.findByText('This link no longer works')).toBeTruthy();
        expect(screen.getByRole('link', { name: 'Sign in to Keepsake' }).getAttribute('href')).toBe('/');
        expect(screen.queryByText(error)).toBeNull();
    });

    // A bare 404 from the wrong host is an HTML page; it must never be printed to the visitor.
    it('never shows a raw HTML error page', async () => {
        mocks.get.mockRejectedValue(apiError(502, { error: '<!doctype html><title>502 Bad Gateway</title>' }));
        renderPage();
        expect(await screen.findByText('This public album link is invalid or no longer available.')).toBeTruthy();
        expect(document.body.textContent).not.toContain('<!doctype');
    });

    it('asks for the code in plain words', async () => {
        mocks.get.mockRejectedValue(apiError(401, { error: 'Access code required', codeRequired: true }));
        renderPage();
        expect(await screen.findByText('Enter the code the person who shared it gave you.')).toBeTruthy();
        expect(screen.getByRole('button', { name: 'View album' })).toBeTruthy();
        expect(screen.getByText('No code? Ask the person who shared it.')).toBeTruthy();
    });

    describe('tiles', () => {
        // The pre-reveal warm-up waits on Image() loads, which jsdom never fires.
        beforeEach(() => {
            vi.stubGlobal('Image', class { onload?: () => void; set src(_url: string) { queueMicrotask(() => this.onload?.()); } });
        });
        afterEach(() => { vi.unstubAllGlobals(); });

        const album = (photos: object[]) => ({ album: { name: 'Trip', photoCount: photos.length }, photos, hasMore: false });

        it('offer the sharp grid image next to the thumbnail, only when the photo has one', async () => {
            mocks.get.mockResolvedValue(album([
                { filename: 'a.jpg', url: '/i/a', thumbnailUrl: 'https://s/a-t.jpg', gridUrl: 'https://s/grid/a.webp', rotation: 90 },
                { filename: 'b.jpg', url: '/i/b', thumbnailUrl: 'https://s/b-t.jpg', rotation: 90, thumbnailRotation: 90 },
                { filename: 'c.jpg', url: '/i/c', thumbnailUrl: 'https://s/c-t.jpg', gridUrl: 'https://s/grid/c.webp', rotation: 180, thumbnailRotation: 90 },
            ]));
            renderPage();
            const [a, b, c] = await screen.findAllByRole('img', { name: /\.jpg$/ }) as HTMLImageElement[];
            expect(a.getAttribute('srcset')).toBe('https://s/a-t.jpg 90w, https://s/grid/a.webp 400w');
            expect(a.style.transform).toBe('rotate(90deg)');
            // No grid image: the thumbnail alone, already turned by its baked 90 degrees.
            expect(b.getAttribute('srcset')).toBeNull();
            expect(b.getAttribute('src')).toBe('https://s/b-t.jpg');
            expect(b.style.transform).toBe('');
            // A baked thumbnail turn can't share one transform with the grid image: grid alone, full turn.
            expect(c.getAttribute('srcset')).toBeNull();
            expect(c.getAttribute('src')).toBe('https://s/grid/c.webp');
            expect(c.style.transform).toBe('rotate(180deg)');
        });

        it('fall back to the proxied thumbnail, without the grid image, when storage misses', async () => {
            mocks.get.mockResolvedValue(album([
                { filename: 'c.jpg', url: '/i/c', thumbnailUrl: 'https://s/c-t.jpg', gridUrl: 'https://s/grid/c.webp', rotation: 180, thumbnailRotation: 90 },
            ]));
            renderPage();
            const tile = await screen.findByRole('img', { name: 'c.jpg' }) as HTMLImageElement;
            fireEvent.error(tile);
            expect(tile.getAttribute('srcset')).toBeNull();
            expect(tile.getAttribute('src')).toBe('/public/photos/tok/thumbnail/c.jpg');
            expect(tile.style.transform).toBe('rotate(90deg)');
        });
    });
});
