import React, { useRef } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
import { forgetGalleryScroll, rememberedGalleryScroll, useGalleryScrollMemory } from './useGalleryScrollMemory';

const TILE = 100;
let body: HTMLElement;

// jsdom has no layout: a tile's top is derived from its index and the body's scrollTop.
const lay = (el: HTMLElement) => {
    const idx = Number(el.dataset.tileId!.replace('p', ''));
    el.getBoundingClientRect = () => ({ top: idx * TILE - body.scrollTop, bottom: 0, left: 0, right: 0, width: TILE, height: TILE, x: 0, y: 0, toJSON: () => ({}) });
};

const Grid: React.FC<{ count: number; hasMore?: boolean; loadMore?: () => void; trackable?: boolean }> = ({ count, hasMore = false, loadMore = () => {}, trackable = true }) => {
    const ref = useRef<HTMLDivElement>(null);
    useGalleryScrollMemory({ gridRef: ref, trackable, photoCount: count, hasMore, loading: false, loadMore });
    return (
        <div ref={ref}>
            {Array.from({ length: count }, (_, i) => (
                <div key={i} data-tile-id={`p${i}`} ref={(el) => { if (el) lay(el); }} />
            ))}
        </div>
    );
};

const mountPage = (props: React.ComponentProps<typeof Grid>) => {
    const host = document.createElement('div');
    body.appendChild(host);
    return render(<Grid {...props} />, { container: host });
};

const scrollTo = (top: number) => {
    body.scrollTop = top;
    // The tile under the viewport centre (innerHeight/2).
    const idx = Math.floor((top + window.innerHeight / 2) / TILE);
    document.elementFromPoint = () => body.querySelector(`[data-tile-id="p${idx}"]`);
    fireEvent.scroll(body);
    act(() => { vi.runAllTimers(); });
};

describe('useGalleryScrollMemory', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0) as unknown as number);
        vi.stubGlobal('cancelAnimationFrame', (id: number) => clearTimeout(id));
        forgetGalleryScroll();
        body = document.createElement('div');
        body.className = 'pt-body';
        body.style.overflowY = 'auto';
        document.body.appendChild(body);
        body.scrollTop = 0;
    });
    afterEach(() => {
        body.remove();
        vi.unstubAllGlobals();
        vi.useRealTimers();
    });

    it('puts the same tile back on screen after leaving and returning', () => {
        const first = mountPage({ count: 50 });
        scrollTo(1500);
        const saved = rememberedGalleryScroll();
        expect(saved).not.toBeNull();
        first.unmount();
        body.scrollTop = 0; // the content vanished, the scroller clamped
        mountPage({ count: 50 });
        const tile = body.querySelector<HTMLElement>(`[data-tile-id="${saved!.id}"]`)!;
        expect(tile.getBoundingClientRect().top).toBeCloseTo(saved!.top, 0);
        expect(body.scrollTop).toBeGreaterThan(1000);
    });

    it('pages in more photos until the remembered one exists', () => {
        const first = mountPage({ count: 50 });
        scrollTo(3000);
        const saved = rememberedGalleryScroll()!;
        first.unmount();
        body.scrollTop = 0;
        const loadMore = vi.fn();
        const view = mountPage({ count: 10, hasMore: true, loadMore });
        expect(loadMore).toHaveBeenCalled();
        expect(body.scrollTop).toBe(0);
        view.rerender(<Grid count={50} hasMore loadMore={loadMore} />);
        expect(body.querySelector(`[data-tile-id="${saved.id}"]`)!.getBoundingClientRect().top).toBeCloseTo(saved.top, 0);
    });

    it('stays at the top when nothing was remembered or the photo is gone', () => {
        mountPage({ count: 50 });
        expect(body.scrollTop).toBe(0);
    });

    it('forgets the position once scrolled back to the top, and outside the Days view', () => {
        const first = mountPage({ count: 50 });
        scrollTo(1500);
        scrollTo(0);
        expect(rememberedGalleryScroll()).toBeNull();
        scrollTo(1500);
        first.rerender(<Grid count={50} trackable={false} />);
        scrollTo(1600);
        expect(rememberedGalleryScroll()).toBeNull();
    });
});
