import { describe, expect, it } from 'vitest';
import { manifestToAlbumResponse } from './snapshotAdapter';

describe('manifestToAlbumResponse', () => {
    const base = 'https://acct.web.core.windows.net/s/tok/';

    it('maps cached photos to the public-album payload with absolute static URLs', () => {
        const res = manifestToAlbumResponse({
            name: 'Trip', total: 3, cached: 2,
            photos: [{ n: 'a b.jpg', rot: 90, trot: 0, t: 't/0.jpg', p: 'p/0.jpg' }, { n: 'v.mp4', video: true, t: 't/1.jpg' }],
        }, base, 'tok');
        expect(res.album).toEqual({ name: 'Trip', photoCount: 3 });
        expect(res.hasMore).toBe(false);
        expect(res.photos[0]).toMatchObject({
            filename: 'a b.jpg', rotation: 90, thumbnailRotation: 0,
            thumbnailUrl: `${base}t/0.jpg`, previewUrl: `${base}p/0.jpg`,
            url: '/public/photos/tok/image/a%20b.jpg',
        });
        expect(res.photos[1].previewUrl).toBeUndefined();
    });
});
