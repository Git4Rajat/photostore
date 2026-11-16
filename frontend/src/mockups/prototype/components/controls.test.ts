import { afterEach, describe, expect, it, vi } from 'vitest';
import { galleryTileKey } from './controls';

describe('gallery tile size', () => {
    afterEach(() => { vi.unstubAllGlobals(); });

    // A size picked in a wide desktop window must never make a phone two across.
    it('keeps phone and desktop sizes apart', () => {
        vi.stubGlobal('innerWidth', 390);
        expect(galleryTileKey()).toBe('photostore.galleryTileSize.v2.phone');
        vi.stubGlobal('innerWidth', 1280);
        expect(galleryTileKey()).toBe('photostore.galleryTileSize.v2.wide');
    });
});
