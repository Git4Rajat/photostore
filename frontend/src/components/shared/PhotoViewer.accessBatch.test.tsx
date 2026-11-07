import React from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ post: vi.fn() }));

vi.mock('../../services/apiClient', () => ({ post: mocks.post, resolveApiUrl: (p: string) => p }));
vi.mock('../../services/authClient', () => ({ getAccessToken: () => 'token', isAuthEnabled: () => true }));
vi.mock('../../services/imageClient', () => ({ fetchProtectedBlobUrl: vi.fn(async () => 'blob:x') }));

import PhotoViewer, { ViewerPhoto } from './PhotoViewer';

const photos: ViewerPhoto[] = Array.from({ length: 600 }, (_, i) => ({
    filename: `p${i}.jpg`,
    url: `/api/photos/image/p${i}.jpg`,
    thumbnailUrl: `/api/photos/thumbnail/p${i}.jpg`,
    previewUrl: `/api/photos/preview/p${i}.jpg`,
}));

const Host: React.FC<{ start: number; api: { go?: (i: number) => void } }> = ({ start, api }) => {
    const [index, setIndex] = React.useState(start);
    api.go = setIndex;
    return <PhotoViewer photos={photos} index={index} onClose={() => undefined} onIndexChange={setIndex} />;
};

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

describe('PhotoViewer signed-URL resolution', () => {
    beforeEach(() => {
        mocks.post.mockReset();
        mocks.post.mockImplementation(async (_url: string, body: { kind: string; filenames: string[] }) => ({
            urls: Object.fromEntries(body.filenames.map((f) => [f, `https://acct.blob.core.windows.net/${body.kind}/${f}?sig=x`])),
        }));
    });
    afterEach(() => cleanup());

    it('resolves a whole window in one call per kind and does not call again while stepping through it', async () => {
        const api: { go?: (i: number) => void } = {};
        render(<Host start={100} api={api} />);
        await flush();
        const initial = mocks.post.mock.calls.length;
        expect(initial).toBeLessThanOrEqual(3);
        const sizes = mocks.post.mock.calls.map(([, body]) => body.filenames.length);
        expect(Math.max(...sizes)).toBeGreaterThan(150);

        for (let i = 101; i <= 160; i += 1) {
            act(() => api.go?.(i));
            await flush();
        }
        expect(mocks.post.mock.calls.length).toBe(initial);
    });

    it('requests the next window only when nearing the edge of the resolved one', async () => {
        const api: { go?: (i: number) => void } = {};
        render(<Host start={100} api={api} />);
        await flush();
        const initial = mocks.post.mock.calls.length;
        act(() => api.go?.(100 + 150 - 10)); // inside the refill margin of the window's far edge
        await flush();
        expect(mocks.post.mock.calls.length).toBeGreaterThan(initial);
        expect(mocks.post.mock.calls.length - initial).toBeLessThanOrEqual(3);
    });
});
