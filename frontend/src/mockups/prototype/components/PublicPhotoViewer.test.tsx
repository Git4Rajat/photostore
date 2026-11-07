import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import PublicPhotoViewer from './PublicPhotoViewer';

const photos = [
    { filename: 'a.jpg', url: 'https://x/a.jpg', previewUrl: 'https://x/a-preview.jpg', rawFullPreviewUrl: 'https://x/a-full.jpg' },
    { filename: 'b.jpg', url: 'https://x/b.jpg', previewUrl: 'https://x/b-preview.jpg' },
    { filename: 'c.mp4', url: 'https://x/c.mp4', thumbnailUrl: 'https://x/c-thumb.jpg' },
];

describe('PublicPhotoViewer', () => {
    it('shows the preview, steps with the keyboard and closes on Escape', () => {
        const onClose = vi.fn();
        const onIndexChange = vi.fn();
        render(<PublicPhotoViewer photos={photos} index={0} onClose={onClose} onIndexChange={onIndexChange} />);
        expect(screen.getByAltText('a.jpg').getAttribute('src')).toBe('https://x/a-preview.jpg');
        expect(screen.getByText('1 / 3')).toBeTruthy();
        fireEvent.keyDown(document, { key: 'ArrowRight' });
        expect(onIndexChange).toHaveBeenCalledWith(1);
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(onClose).toHaveBeenCalled();
    });

    it('does not step past either end', () => {
        const onIndexChange = vi.fn();
        render(<PublicPhotoViewer photos={photos} index={0} onClose={vi.fn()} onIndexChange={onIndexChange} />);
        fireEvent.keyDown(document, { key: 'ArrowLeft' });
        expect(onIndexChange).not.toHaveBeenCalled();
    });

    it('switches to the full-resolution URL on FR and plays videos', () => {
        const { rerender } = render(<PublicPhotoViewer photos={photos} index={0} onClose={vi.fn()} onIndexChange={vi.fn()} />);
        fireEvent.click(screen.getByLabelText('Full resolution'));
        expect(screen.getByAltText('a.jpg').getAttribute('src')).toBe('https://x/a-full.jpg');
        rerender(<PublicPhotoViewer photos={photos} index={2} onClose={vi.fn()} onIndexChange={vi.fn()} />);
        expect(document.querySelector('video')?.getAttribute('src')).toBe('https://x/c.mp4');
        expect(screen.queryByLabelText('Full resolution')).toBeNull();
    });
});
