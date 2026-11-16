import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
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

});
