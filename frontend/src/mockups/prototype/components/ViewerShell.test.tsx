import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ViewerShell, { liveCorner, type ViewerShellProps } from './ViewerShell';

const props = (overrides: Partial<ViewerShellProps> = {}): ViewerShellProps => ({
    photoKey: 'IMG_1.HEIC', filename: 'IMG_1.HEIC', title: 'IMG_1.HEIC', index: 0, count: 2,
    onClose: vi.fn(), onStep: vi.fn(), media: { url: 'https://x/preview.jpg', loading: false, progress: 0 },
    fullRes: false, onToggleFullRes: vi.fn(), showFullResControl: true, onDownloadOriginal: vi.fn(), bar: null,
    ...overrides,
});

describe('ViewerShell Live Photos', () => {
    it('plays the motion clip over the still once, then returns to the still', () => {
        render(<ViewerShell {...props({ liveSrc: 'https://x/IMG_1.MOV' })} />);
        expect(document.querySelector('video')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'Play Live Photo' }));
        const clip = document.querySelector('video');
        expect(clip?.getAttribute('src')).toBe('https://x/IMG_1.MOV');
        expect(screen.getByAltText('IMG_1.HEIC')).toBeTruthy();
        fireEvent.ended(clip!);
        expect(document.querySelector('video')).toBeNull();
    });

    it('stops when the viewer moves to another photo', () => {
        const { rerender } = render(<ViewerShell {...props({ liveSrc: 'https://x/IMG_1.MOV' })} />);
        fireEvent.click(screen.getByRole('button', { name: 'Play Live Photo' }));
        rerender(<ViewerShell {...props({ photoKey: 'IMG_2.HEIC', filename: 'IMG_2.HEIC', liveSrc: 'https://x/IMG_2.MOV' })} />);
        expect(document.querySelector('video')).toBeNull();
    });

    it('offers no LIVE control for an ordinary photo or a video', () => {
        const { rerender } = render(<ViewerShell {...props()} />);
        expect(screen.queryByRole('button', { name: 'Play Live Photo' })).toBeNull();
        rerender(<ViewerShell {...props({ liveSrc: 'https://x/IMG_1.MOV', videoSrc: 'https://x/trip.mov' })} />);
        expect(screen.queryByRole('button', { name: 'Play Live Photo' })).toBeNull();
    });
});

describe('ViewerShell video rotation', () => {
    const video = (overrides: Partial<ViewerShellProps> = {}) => props({
        photoKey: 'clip.mp4', filename: 'clip.mp4', title: 'clip.mp4', videoSrc: 'https://x/clip.mp4',
        videoPoster: 'https://x/clip-thumb.jpg', showFullResControl: false, ...overrides,
    });

    it('offers rotate (not zoom) for a video and keeps the native player while upright', () => {
        render(<ViewerShell {...video()} />);
        const toolbar = screen.getByRole('toolbar', { name: 'Video controls' });
        expect(toolbar.className).toContain('always');
        expect(screen.queryByRole('button', { name: 'Zoom in' })).toBeNull();
        expect(document.querySelector('video')?.hasAttribute('controls')).toBe(true);
        expect(screen.queryByRole('group', { name: 'Video playback' })).toBeNull();
    });

    it('turns the picture, swaps in upright controls, and reports the rotation to save', () => {
        const onRotate = vi.fn();
        render(<ViewerShell {...video({ onRotate })} />);
        fireEvent.click(screen.getByRole('button', { name: 'Rotate right' }));
        const el = document.querySelector('video')!;
        expect(el.style.transform).toBe('rotate(90deg)');
        expect(el.hasAttribute('controls')).toBe(false);
        expect(screen.getByRole('group', { name: 'Video playback' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Play video' })).toBeTruthy();
        expect(onRotate).toHaveBeenCalledWith(90, 0);
    });

    it('opens a video at its saved rotation', () => {
        render(<ViewerShell {...video({ initialRotation: 270 })} />);
        expect(document.querySelector('video')!.style.transform).toBe('rotate(270deg)');
    });
});

describe('liveCorner', () => {
    it('puts the LIVE button on the drawn photo, not the stage', () => {
        // A 3:4 portrait still in a wide stage is centred with side margins.
        expect(liveCorner({ w: 1536, h: 2048 }, { w: 861, h: 538 }, 0)).toEqual({ left: 239, top: 10 });
        expect(liveCorner({ w: 1536, h: 2048 }, { w: 861, h: 538 }, 90)).toEqual({ left: 172, top: 77 });
        expect(liveCorner(null, { w: 861, h: 538 }, 0)).toBeUndefined();
    });
});

describe('ViewerShell wheel zoom', () => {
    const scale = () => Number(/scale\(([\d.]+)\)/.exec(screen.getByAltText('IMG_1.HEIC').getAttribute('style') || '')?.[1]);

    it('zooms in and back out with a plain mouse wheel over the photo', () => {
        render(<ViewerShell {...props()} />);
        const stage = document.querySelector('.pt-viewer-stage-wrap')!;
        fireEvent.wheel(stage, { deltaY: -100, clientX: 400, clientY: 300 });
        fireEvent.wheel(stage, { deltaY: -100, clientX: 400, clientY: 300 });
        expect(scale()).toBeGreaterThan(1.5);
        for (let i = 0; i < 10; i += 1) fireEvent.wheel(stage, { deltaY: 100, clientX: 400, clientY: 300 });
        expect(scale()).toBe(1);
    });
});
