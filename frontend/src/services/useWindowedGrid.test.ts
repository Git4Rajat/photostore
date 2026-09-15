import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { renderHook, render } from '@testing-library/react';
import { computeWindowRange, useWindowedGrid } from './useWindowedGrid';

describe('computeWindowRange', () => {
    const base = {
        itemCount: 1000,
        columnCount: 4,
        rowHeight: 100,
        rowGap: 10,
        overscanRows: 2,
        viewportHeight: 800,
    };
    // rowPitch = 110; 250 total rows for 1000 items / 4 columns.

    it('returns an empty range for zero items', () => {
        const range = computeWindowRange({ ...base, itemCount: 0, containerTopOffset: 0 });
        expect(range).toEqual({ firstRow: 0, lastRowExclusive: 0, totalRows: 0 });
    });

    it('starts at row 0 (clamped) when the container top is at or below the viewport top', () => {
        const range = computeWindowRange({ ...base, containerTopOffset: 0 });
        expect(range.firstRow).toBe(0);
        // viewBottom = 0 + 800 + 2*110 = 1020 -> ceil(1020/110) = 10
        expect(range.lastRowExclusive).toBe(10);
        expect(range.totalRows).toBe(250);
    });

    it('includes overscan rows above the first fully-visible row', () => {
        // Scrolled so row 20 (y = 2200) is at the top of the viewport.
        const range = computeWindowRange({ ...base, containerTopOffset: -2200 });
        // viewTop = 2200 - 2*110 = 1980 -> floor(1980/110) = 18
        expect(range.firstRow).toBe(18);
    });

    it('clamps firstRow so it never goes negative even with a large overscan', () => {
        const range = computeWindowRange({ ...base, containerTopOffset: -50, overscanRows: 50 });
        expect(range.firstRow).toBe(0);
    });

    it('clamps lastRowExclusive to totalRows near the end of the list', () => {
        // Scroll almost to the very bottom of the content.
        const totalHeight = 250 * 100 + 249 * 10; // 27490
        const range = computeWindowRange({ ...base, containerTopOffset: -(totalHeight - 800) });
        expect(range.lastRowExclusive).toBe(250);
        expect(range.firstRow).toBeLessThan(250);
    });

    it('keeps lastRowExclusive at least firstRow + 1 for a tall single-screen viewport', () => {
        const range = computeWindowRange({ ...base, viewportHeight: 50, overscanRows: 0, containerTopOffset: 0 });
        expect(range.lastRowExclusive).toBeGreaterThan(range.firstRow);
    });
});

describe('useWindowedGrid', () => {
    it('renders a bounded initial slice instead of the full item list when items are large', () => {
        const items = Array.from({ length: 1000 }, (_, i) => ({ filename: `photo-${i}.jpg` }));
        const { result } = renderHook(() => useWindowedGrid({ items, getKey: (p) => p.filename }));
        // No real layout occurs under renderHook (no attached DOM refs), so the
        // hook falls back to its initial guess -- this just asserts windowing
        // is active at all, not exact row counts (that's computeWindowRange's job).
        expect(result.current.visibleItems.length).toBeLessThan(items.length);
        expect(result.current.isVirtualized).toBe(true);
    });

    it('renders every item when disabled', () => {
        const items = Array.from({ length: 50 }, (_, i) => ({ filename: `photo-${i}.jpg` }));
        const { result } = renderHook(() => useWindowedGrid({ items, getKey: (p) => p.filename, enabled: false }));
        expect(result.current.visibleItems).toEqual(items);
        expect(result.current.isVirtualized).toBe(false);
    });

    it('marks an item as already-seen once it has been part of a rendered slice', () => {
        // `result.current` is always read after effects have flushed, so by
        // the time this assertion runs, the hook's own useLayoutEffect has
        // already recorded 'a.jpg' as seen -- this is what a caller relies on
        // to skip the entrance animation the *next* time a tile scrolls back
        // into view (validated live via Playwright, not practical to observe
        // the "still new" instant in a pure hook test since that instant is
        // strictly before any effect commits).
        const items = [{ filename: 'a.jpg' }];
        const { result } = renderHook(() => useWindowedGrid({ items, getKey: (p) => p.filename, enabled: false }));
        expect(result.current.shouldAnimateEntrance('a.jpg')).toBe(false);
    });

    it('bails out instead of crashing when live layout measurements never settle', () => {
        // Regression test: recompute() trusts getComputedStyle/getBoundingClientRect
        // to converge across repeated calls. If real browser layout ever fails to
        // (e.g. a measurement that ping-pongs between two states), the
        // useLayoutEffect-with-no-deps that drives recompute() would otherwise
        // re-trigger itself synchronously forever and React would throw
        // "Maximum update depth exceeded", crashing the whole page. Force that
        // oscillation here and assert it degrades (console.error) instead of throwing.
        const items = Array.from({ length: 40 }, (_, i) => ({ filename: `photo-${i}.jpg` }));

        const realGetComputedStyle = window.getComputedStyle;
        let call = 0;
        const getComputedStyleSpy = vi.spyOn(window, 'getComputedStyle').mockImplementation((el, ...rest) => {
            const real = realGetComputedStyle(el, ...rest);
            call += 1;
            const columns = call % 2 === 0 ? '100px 100px 100px' : '100px 100px 100px 100px';
            return new Proxy(real, {
                get(target, prop) {
                    if (prop === 'gridTemplateColumns') {
                        return columns;
                    }
                    if (prop === 'rowGap') {
                        return '10px';
                    }
                    return Reflect.get(target, prop);
                },
            });
        });

        let rectCall = 0;
        const rectSpy = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function mockRect() {
            rectCall += 1;
            const height = rectCall % 2 === 0 ? 300 : 301;
            return {
                top: 0, left: 0, right: 0, bottom: height, width: 400, height,
                x: 0, y: 0, toJSON: () => ({}),
            } as DOMRect;
        });

        const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

        function Harness() {
            const { containerRef, innerRef, visibleItems } = useWindowedGrid({
                items, getKey: (p) => p.filename,
            });
            return React.createElement('div', { ref: containerRef },
                React.createElement('div', { ref: innerRef },
                    visibleItems.map((item) => React.createElement('div', { key: item.filename }))));
        }

        expect(() => render(React.createElement(Harness))).not.toThrow();
        expect(consoleErrorSpy.mock.calls.some(([msg]) => typeof msg === 'string' && msg.includes('recompute() re-triggered itself too many times'))).toBe(true);

        getComputedStyleSpy.mockRestore();
        rectSpy.mockRestore();
        consoleErrorSpy.mockRestore();
    });
});
