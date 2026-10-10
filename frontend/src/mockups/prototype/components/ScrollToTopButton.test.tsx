import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { ScrollToTopButton } from './ScrollToTopButton';

// The component finds its scroll container via document.querySelector('.pt-body')
// rather than a prop/ref, matching how it's actually mounted in PrototypeApp.tsx
// (a sibling of the real .pt-body, not its parent) -- so tests render a stand-in
// .pt-body alongside it, same as production.
const renderWithScrollBody = () => {
    const { container } = render(
        <div>
            <div className="pt-body" />
            <ScrollToTopButton />
        </div>,
    );
    return container.querySelector('.pt-body') as HTMLElement;
};

const scrollTo = (el: HTMLElement, top: number) => {
    Object.defineProperty(el, 'scrollTop', { value: top, writable: true, configurable: true });
    fireEvent.scroll(el);
};

describe('ScrollToTopButton', () => {
    beforeEach(() => {
        vi.useFakeTimers();
    });
    afterEach(() => {
        vi.useRealTimers();
    });

    it('stays hidden (not clickable) until scrolled past the threshold and settled', () => {
        const body = renderWithScrollBody();
        const button = screen.getByRole('button', { name: 'Scroll to top' });
        expect(button.className).not.toContain('visible');

        scrollTo(body, 500);
        // Still hidden immediately after a scroll event -- only appears once it settles.
        expect(button.className).not.toContain('visible');

        act(() => { vi.advanceTimersByTime(150); });
        expect(button.className).toContain('visible');
    });

    it('hides again (and clears the pending reveal) while scrolling is still in flight', () => {
        const body = renderWithScrollBody();
        const button = screen.getByRole('button', { name: 'Scroll to top' });

        scrollTo(body, 500);
        act(() => { vi.advanceTimersByTime(150); });
        expect(button.className).toContain('visible');

        scrollTo(body, 520); // another scroll tick -- hides until it settles again
        expect(button.className).not.toContain('visible');
        act(() => { vi.advanceTimersByTime(150); });
        expect(button.className).toContain('visible');
    });

    it('never shows once scrolled back above the threshold', () => {
        const body = renderWithScrollBody();
        const button = screen.getByRole('button', { name: 'Scroll to top' });

        scrollTo(body, 500);
        act(() => { vi.advanceTimersByTime(150); });
        expect(button.className).toContain('visible');

        scrollTo(body, 100);
        expect(button.className).not.toContain('visible');
        act(() => { vi.advanceTimersByTime(150); });
        expect(button.className).not.toContain('visible'); // no delayed reveal below the threshold
    });

    it('scrolls the .pt-body container (not the window) to the top on click', () => {
        const body = renderWithScrollBody();
        body.scrollTo = vi.fn();
        const button = screen.getByRole('button', { name: 'Scroll to top' });

        fireEvent.click(button);

        expect(body.scrollTo).toHaveBeenCalledWith({ top: 0, behavior: 'smooth' });
    });
});
