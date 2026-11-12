import React, { useRef } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ grids: new Map<string, string>(), thumbs: new Map<string, string>() }));
vi.mock('../../services/localSortIndex', () => ({
    getCachedSortGrid: (filename: string) => mocks.grids.get(filename) || '',
    getCachedSortThumb: (filename: string) => mocks.thumbs.get(filename) || '',
}));
vi.mock('../../services/mediaToken', () => ({
    getCachedMediaToken: () => ({ baseUrl: 'https://store.invalid/thumbnails', sas: 'sig=1' }),
    getMediaToken: vi.fn(),
    thumbnailUrlForBlob: (blob: string | undefined, token: { baseUrl: string; sas: string } | null) => (
        blob && token ? `${token.baseUrl}/${blob}?${token.sas}` : ''),
    previewUrlForBlob: () => '',
    imageUrlForBlob: () => '',
}));

import { tileImage, useTileSizes } from './media';
import PhotoGrid from './components/PhotoGrid';
import type { Photo } from './types';

vi.mock('./store', () => ({
    useStore: () => ({
        selection: [], toggleSelect: vi.fn(), selectMany: vi.fn(), openViewer: vi.fn(),
        selectMode: false, setSelectMode: vi.fn(),
    }),
}));

const photo = (overrides: Partial<Photo> = {}): Photo => ({
    id: 'IMG_1.jpg', filename: 'IMG_1.jpg', swatch: 's1', dateLabel: '', year: 2026, rating: 0,
    liked: false, placeId: null, personIds: [], tags: [], ...overrides,
});
const THUMB = 'https://store.invalid/thumbnails/abc?sig=1';
const GRID = 'https://store.invalid/thumbnails/grid/abc.webp?sig=1';

afterEach(() => {
    cleanup();
    mocks.grids.clear();
    mocks.thumbs.clear();
});

describe('tileImage', () => {
    it('keeps the thumbnail alone until the photo has a grid image', () => {
        expect(tileImage(photo({ rotation: 90, thumbnailRotation: 90 }), THUMB)).toEqual({ src: THUMB, style: undefined });
        expect(tileImage(photo({ rotation: 180 }), THUMB)).toEqual({ src: THUMB, style: { transform: 'rotate(180deg)' } });
        expect(tileImage(photo(), undefined)).toBeNull();
    });

    it('offers the grid image next to the thumbnail, declared by each short edge', () => {
        mocks.grids.set('IMG_1.jpg', 'grid/abc.webp');
        expect(tileImage(photo({ rotation: 270 }), THUMB)).toEqual({
            src: THUMB, srcSet: `${THUMB} 90w, ${GRID} 400w`, style: { transform: 'rotate(270deg)' },
        });
    });

    it('prefers a summary gridBlob and works before the thumbnail resolves', () => {
        expect(tileImage(photo({ gridBlob: 'grid/abc.webp' }), undefined)).toEqual({ src: GRID, style: undefined });
    });

    it('shows the grid image alone, turned by the full rotation, when the thumbnail has a baked turn', () => {
        // The thumbnail bakes in 90 deg; the grid image (like the preview) does not, so they can't share one transform.
        const image = tileImage(photo({ gridBlob: 'grid/abc.webp', rotation: 90, thumbnailRotation: 90 }), THUMB);
        expect(image).toEqual({ src: GRID, style: { transform: 'rotate(90deg)' } });
    });
});

describe('useTileSizes', () => {
    const Grid: React.FC<{ width: number }> = ({ width }) => {
        const ref = useRef<HTMLDivElement>(null);
        const sizes = useTileSizes(ref, 'first');
        return (
            <div ref={ref} data-sizes={sizes ?? 'none'} data-testid="grid">
                <div ref={(el) => { if (el) el.getBoundingClientRect = () => ({ width } as DOMRect); }} />
            </div>
        );
    };

    it('reports the first tile width as sizes, or nothing before layout', () => {
        render(<Grid width={168.4} />);
        expect(screen.getByTestId('grid').dataset.sizes).toBe('168px');
        cleanup();
        render(<Grid width={0} />);
        expect(screen.getByTestId('grid').dataset.sizes).toBe('none');
    });
});

describe('PhotoGrid', () => {
    beforeEach(() => {
        mocks.thumbs.set('IMG_1.jpg', 'abc');
        mocks.thumbs.set('IMG_2.jpg', 'def');
        mocks.grids.set('IMG_1.jpg', 'grid/abc.webp');
    });

    it('renders srcset only for photos that have a grid image', () => {
        render(<PhotoGrid photos={[photo(), photo({ id: 'IMG_2.jpg', filename: 'IMG_2.jpg' })]} />);
        const [sharp, plain] = screen.getAllByRole('img') as HTMLImageElement[];
        expect(sharp.getAttribute('srcset')).toBe(`${THUMB} 90w, ${GRID} 400w`);
        expect(sharp.getAttribute('src')).toBe(THUMB);
        expect(plain.getAttribute('srcset')).toBeNull();
        expect(plain.getAttribute('src')).toBe('https://store.invalid/thumbnails/def?sig=1');
    });
});
