import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../media', () => ({
    tileImage: () => null,
    usePhotoThumbnails: () => ({}),
    useTileSizes: () => undefined,
}));
vi.mock('../store', () => ({
    isVideoFilename: () => false,
    useStore: () => ({
        selection: [], toggleSelect: vi.fn(), selectMany: vi.fn(), openViewer: vi.fn(),
        selectMode: false, setSelectMode: vi.fn(),
    }),
}));

import PhotoGrid, { dayHeading } from './PhotoGrid';
import type { Photo } from '../types';

const photo = (filename: string, captureDate: string | null): Photo => ({
    id: filename, filename, swatch: 's1', dateLabel: '', year: 0, rating: 0, liked: false,
    placeId: null, personIds: [], tags: [], captureDate,
});

describe('PhotoGrid day headings', () => {
    afterEach(cleanup);

    it('puts one heading above each capture day, in order', () => {
        const photos = [
            photo('a.jpg', '2024-10-10T22:41:00'),
            photo('b.jpg', '2024-10-10T08:00:00'),
            photo('c.jpg', '2024-01-27T12:00:00'),
            photo('d.jpg', null),
        ];
        render(<PhotoGrid photos={photos} dayHeadings />);
        const headings = screen.getAllByRole('heading').map((h) => h.textContent);
        expect(headings).toEqual([dayHeading(photos[0]), dayHeading(photos[2]), 'Undated']);
        expect(headings[0]).toMatch(/2024/);
    });

    it('shows no headings unless asked', () => {
        render(<PhotoGrid photos={[photo('a.jpg', '2024-10-10T22:41:00')]} />);
        expect(screen.queryAllByRole('heading')).toHaveLength(0);
    });
});
