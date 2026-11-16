import { describe, expect, it } from 'vitest';
import { composeTileImage, thumbnailTileStyle } from './tileImage';

describe('composeTileImage', () => {
    it('keeps the thumbnail alone until there is a grid image', () => {
        expect(composeTileImage('t.jpg', undefined, 90, 90)).toEqual({ src: 't.jpg', style: undefined });
        expect(composeTileImage('t.jpg', undefined, 180, 90)).toEqual({ src: 't.jpg', style: { transform: 'rotate(90deg)' } });
        expect(composeTileImage(undefined, undefined)).toBeNull();
    });

    it('lists both in srcset by short edge when the thumbnail has no baked turn', () => {
        expect(composeTileImage('t.jpg', 'g.webp', -90)).toEqual({
            src: 't.jpg', srcSet: 't.jpg 90w, g.webp 400w', style: { transform: 'rotate(270deg)' },
        });
    });

    it('shows the grid image alone when the thumbnail is missing or carries a baked turn', () => {
        expect(composeTileImage(undefined, 'g.webp')).toEqual({ src: 'g.webp', style: undefined });
        expect(composeTileImage('t.jpg', 'g.webp', 90, 90)).toEqual({ src: 'g.webp', style: { transform: 'rotate(90deg)' } });
    });

    it('turns a thumbnail by only what was rotated beyond its baked turn', () => {
        expect(thumbnailTileStyle(0, 270)).toEqual({ transform: 'rotate(90deg)' });
        expect(thumbnailTileStyle(90, 90)).toBeUndefined();
    });
});
