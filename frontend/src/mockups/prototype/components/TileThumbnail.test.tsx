import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ token: { baseUrl: 'https://acct/thumbnails', sas: 'sig=NEW' } as null | { baseUrl: string; sas: string } }));
vi.mock('../../../services/mediaToken', () => ({
    getCachedMediaToken: () => mocks.token,
    getMediaToken: vi.fn(async () => mocks.token),
    thumbnailUrlForBlob: () => '',
    previewUrlForBlob: () => '',
    imageUrlForBlob: () => '',
    resignMediaUrls: vi.fn(async (src: string, srcSet?: string) => {
        const token = mocks.token;
        if (!token) return null;
        const swap = (url: string) => (url.startsWith(`${token.baseUrl}/`) ? `${url.split('?')[0]}?${token.sas}` : url);
        const next = { src: swap(src), srcSet: srcSet?.split(', ').map((e) => { const [u, d] = e.split(' '); return `${swap(u)} ${d}`; }).join(', ') };
        return next.src === src && next.srcSet === srcSet ? null : next;
    }),
}));

import { TileThumbnail } from './PhotoGrid';

const OLD = 'https://acct/thumbnails/abc?sig=OLD';
const NEW = 'https://acct/thumbnails/abc?sig=NEW';

describe('tile images when the media token expires', () => {
    beforeEach(() => { mocks.token = { baseUrl: 'https://acct/thumbnails', sas: 'sig=NEW' }; });
    afterEach(cleanup);

    it('retries once with the current signature instead of going blank', async () => {
        render(<TileThumbnail src={OLD} srcSet={`${OLD} 90w, https://acct/thumbnails/grid/abc.webp?sig=OLD 400w`} alt="a.jpg" />);
        const img = screen.getByRole('img', { name: 'a.jpg' }) as HTMLImageElement;
        await act(async () => { fireEvent.error(img); });
        const retried = screen.getByRole('img', { name: 'a.jpg' }) as HTMLImageElement;
        expect(retried.getAttribute('src')).toBe(NEW);
        expect(retried.getAttribute('srcset')).toBe(`${NEW} 90w, https://acct/thumbnails/grid/abc.webp?sig=NEW 400w`);
        expect(retried.style.visibility).toBe('');
        // Still failing with a fresh signature: the image really is missing.
        await act(async () => { fireEvent.error(retried); });
        expect(screen.queryByRole('img')).toBeNull();
    });

    it('gives up at once on a URL that already has the current signature (thumbnail not made yet)', async () => {
        render(<TileThumbnail src={NEW} alt="a.jpg" />);
        await act(async () => { fireEvent.error(screen.getByRole('img', { name: 'a.jpg' })); });
        expect(screen.queryByRole('img')).toBeNull();
    });

    it('starts over when the tile is given a new URL', async () => {
        const { rerender } = render(<TileThumbnail src={NEW} alt="a.jpg" />);
        await act(async () => { fireEvent.error(screen.getByRole('img', { name: 'a.jpg' })); });
        expect(screen.queryByRole('img')).toBeNull();
        rerender(<TileThumbnail src="https://acct/thumbnails/ready?sig=NEW" alt="a.jpg" />);
        expect(screen.getByRole('img', { name: 'a.jpg' }).getAttribute('src')).toBe('https://acct/thumbnails/ready?sig=NEW');
    });
});
