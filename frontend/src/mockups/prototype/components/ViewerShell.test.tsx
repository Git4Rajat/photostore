import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import ViewerShell, { liveCorner, TOOLS_HIDE_MS, type ViewerShellProps } from './ViewerShell';

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
        expect(screen.getByRole('toolbar', { name: 'Video controls' })).toBeTruthy();
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

const toolsShown = () => document.querySelector('.pt-viewer')!.classList.contains('tools-visible');
const touch = (el: Element, type: 'touchStart' | 'touchMove' | 'touchEnd', x: number, y: number) => {
    const point = [{ clientX: x, clientY: y }];
    fireEvent[type](el, type === 'touchEnd' ? { touches: [], changedTouches: point } : { touches: point, changedTouches: point });
};
const tapAt = (el: Element, x = 400, y = 300) => { touch(el, 'touchStart', x, y); touch(el, 'touchEnd', x, y); };
const zoomIn = () => {
    const stage = document.querySelector('.pt-viewer-stage-wrap')!;
    fireEvent.wheel(stage, { deltaY: -100, clientX: 400, clientY: 300 });
    fireEvent.wheel(stage, { deltaY: -100, clientX: 400, clientY: 300 });
};

describe('ViewerShell floating panel', () => {
    afterEach(() => { vi.useRealTimers(); });

    it('a tap shows it, and it hides itself after a few seconds', () => {
        vi.useFakeTimers();
        render(<ViewerShell {...props()} />);
        expect(toolsShown()).toBe(false);
        fireEvent.click(screen.getByAltText('IMG_1.HEIC'));
        expect(toolsShown()).toBe(true);
        act(() => { vi.advanceTimersByTime(TOOLS_HIDE_MS - 100); });
        expect(toolsShown()).toBe(true);
        act(() => { vi.advanceTimersByTime(200); });
        expect(toolsShown()).toBe(false);
    });

    it('using it keeps it up', () => {
        vi.useFakeTimers();
        render(<ViewerShell {...props()} />);
        fireEvent.click(screen.getByAltText('IMG_1.HEIC'));
        act(() => { vi.advanceTimersByTime(TOOLS_HIDE_MS - 500); });
        vi.setSystemTime(Date.now() + 1000);
        fireEvent.pointerDown(screen.getByRole('toolbar', { name: 'Image controls' }));
        act(() => { vi.advanceTimersByTime(TOOLS_HIDE_MS - 500); });
        expect(toolsShown()).toBe(true);
    });

    it('stays up while a full-resolution download shows its progress', () => {
        vi.useFakeTimers();
        render(<ViewerShell {...props({ fullRes: true, media: { url: 'https://x/preview.jpg', loading: true, progress: 40 } })} />);
        fireEvent.click(screen.getByAltText('IMG_1.HEIC'));
        act(() => { vi.advanceTimersByTime(TOOLS_HIDE_MS * 3); });
        expect(toolsShown()).toBe(true);
    });

    it('a tap on a video shows the same panel, with rotate', () => {
        render(<ViewerShell {...props({ photoKey: 'clip.mp4', filename: 'clip.mp4', videoSrc: 'https://x/clip.mp4', showFullResControl: false })} />);
        expect(toolsShown()).toBe(false);
        fireEvent.click(document.querySelector('video')!);
        expect(toolsShown()).toBe(true);
        expect(screen.getByRole('button', { name: 'Rotate left' })).toBeTruthy();
        fireEvent.click(document.querySelector('video')!);
        expect(toolsShown()).toBe(false);
    });

    it('a tap still toggles it while zoomed in, and a double tap zooms back out', () => {
        render(<ViewerShell {...props()} />);
        zoomIn();
        const photo = document.querySelector('.pt-viewer-photo')!;
        expect(screen.getByAltText('IMG_1.HEIC').getAttribute('style')).toContain('scale(1.');
        tapAt(photo);
        expect(toolsShown()).toBe(true);
        tapAt(photo);
        expect(screen.getByAltText('IMG_1.HEIC').getAttribute('style')).toContain('scale(1)');
    });

    it('its buttons still work while zoomed in', () => {
        render(<ViewerShell {...props()} />);
        zoomIn();
        const zoomOut = screen.getByRole('button', { name: 'Zoom out' });
        const start = new TouchEvent('touchstart', { bubbles: true, cancelable: true });
        Object.defineProperty(start, 'touches', { value: [{ clientX: 10, clientY: 10 }] });
        zoomOut.dispatchEvent(start);
        expect(start.defaultPrevented).toBe(false);
    });

    it('wheel zoom leaves a video alone, so a swipe still steps past it', () => {
        const onStep = vi.fn();
        render(<ViewerShell {...props({ photoKey: 'clip.mp4', filename: 'clip.mp4', videoSrc: 'https://x/clip.mp4', showFullResControl: false, onStep })} />);
        zoomIn();
        const stage = document.querySelector('.pt-viewer-stage')!;
        touch(stage, 'touchStart', 300, 300);
        touch(stage, 'touchEnd', 150, 300);
        expect(onStep).toHaveBeenCalledWith(1);
    });
});

describe('ViewerShell swipe down to close', () => {
    it('closes on a swipe down the photo', () => {
        const onClose = vi.fn();
        render(<ViewerShell {...props({ onClose })} />);
        const stage = document.querySelector('.pt-viewer-stage')!;
        touch(stage, 'touchStart', 200, 100);
        touch(stage, 'touchMove', 205, 180);
        expect((stage as HTMLElement).style.transform).toContain('translateY(80px)');
        touch(stage, 'touchEnd', 205, 260);
        expect(onClose).toHaveBeenCalledTimes(1);
        expect((stage as HTMLElement).style.transform).toBe('');
    });

    it('springs back from a short or sideways drag', () => {
        const onClose = vi.fn();
        const onStep = vi.fn();
        render(<ViewerShell {...props({ onClose, onStep })} />);
        const stage = document.querySelector('.pt-viewer-stage')!;
        touch(stage, 'touchStart', 200, 100);
        touch(stage, 'touchEnd', 200, 150);
        touch(stage, 'touchStart', 300, 100);
        touch(stage, 'touchEnd', 150, 160);
        expect(onClose).not.toHaveBeenCalled();
        expect(onStep).toHaveBeenCalledWith(1);
    });

    it('does not close while zoomed in, where one finger pans', () => {
        const onClose = vi.fn();
        render(<ViewerShell {...props({ onClose })} />);
        zoomIn();
        const stage = document.querySelector('.pt-viewer-stage')!;
        touch(stage, 'touchStart', 200, 100);
        touch(stage, 'touchEnd', 200, 400);
        expect(onClose).not.toHaveBeenCalled();
    });
});

describe('ViewerShell full resolution', () => {
    it('hands back to the caller when the browser cannot draw the original', () => {
        const onFullResFailed = vi.fn();
        render(<ViewerShell {...props({ fullRes: true, media: { url: 'blob:original', loading: false, progress: 100 }, onFullResFailed })} />);
        fireEvent.error(screen.getByAltText('IMG_1.HEIC'));
        expect(onFullResFailed).toHaveBeenCalledTimes(1);
        expect(screen.queryByText(/Unsupported format/)).toBeNull();
    });

    it('still reports an undrawable preview as unsupported', () => {
        render(<ViewerShell {...props({ onFullResFailed: vi.fn() })} />);
        fireEvent.error(screen.getByAltText('IMG_1.HEIC'));
        expect(screen.getByText(/Unsupported format/)).toBeTruthy();
    });
});
