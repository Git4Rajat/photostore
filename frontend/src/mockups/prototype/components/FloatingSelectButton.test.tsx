import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

const store = vi.hoisted(() => ({ selectMode: false, setSelectMode: vi.fn() }));

import { FloatingSelectButton } from './FloatingSelectButton';

const renderOver = (withGrid: boolean, suppressed = false) => {
    const { container } = render(
        <div>
            <div className="pt-body">{withGrid && <div className="pt-grid" />}</div>
            <FloatingSelectButton selectMode={store.selectMode} onToggle={() => store.setSelectMode(!store.selectMode)} suppressed={suppressed} />
        </div>,
    );
    return container.querySelector('.pt-body') as HTMLElement;
};

const scrollTo = (el: HTMLElement, top: number) => {
    Object.defineProperty(el, 'scrollTop', { value: top, writable: true, configurable: true });
    fireEvent.scroll(el);
    act(() => { vi.advanceTimersByTime(150); });
};

describe('FloatingSelectButton', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        store.selectMode = false;
        store.setSelectMode.mockReset();
    });
    afterEach(() => vi.useRealTimers());

    it('appears after scrolling far down on a page with a grid, and starts select mode', () => {
        const body = renderOver(true);
        const button = screen.getByRole('button', { name: 'Select photos' });
        expect(button.className).not.toContain('visible');
        scrollTo(body, 900);
        expect(button.className).toContain('visible');
        fireEvent.click(button);
        expect(store.setSelectMode).toHaveBeenCalledWith(true);
    });

    it('stays hidden near the top and on pages without a photo grid', () => {
        const body = renderOver(true);
        scrollTo(body, 100);
        expect(screen.getByRole('button', { name: 'Select photos' }).className).not.toContain('visible');
    });

    it('does not appear where there is nothing to select', () => {
        const body = renderOver(false);
        scrollTo(body, 900);
        expect(screen.getByRole('button', { name: 'Select photos' }).className).not.toContain('visible');
    });

    it('turns into Done while selecting and leaves select mode when clicked', () => {
        store.selectMode = true;
        const body = renderOver(true);
        scrollTo(body, 900);
        const button = screen.getByRole('button', { name: 'Done selecting' });
        expect(button.className).toContain('visible');
        fireEvent.click(button);
        expect(store.setSelectMode).toHaveBeenCalledWith(false);
    });

    it('only appears over the grid it selects from, never over the People grid', () => {
        const { container } = render(
            <div>
                <div className="pt-body"><div className="pt-people-grid" /></div>
                <FloatingSelectButton selectMode={false} onToggle={() => {}} />
                <FloatingSelectButton selectMode={false} onToggle={() => {}} noun="people" grid=".pt-people-grid" />
            </div>,
        );
        scrollTo(container.querySelector('.pt-body') as HTMLElement, 900);
        expect(screen.getByRole('button', { name: 'Select photos' }).className).not.toContain('visible');
        expect(screen.getByRole('button', { name: 'Select people' }).className).toContain('visible');
    });

    it('marks itself for hiding on touch screens, where long-press starts selecting', () => {
        render(<FloatingSelectButton selectMode={false} onToggle={() => {}} hideOnTouch />);
        expect(screen.getByRole('button', { name: 'Select photos' }).className).toContain('touch-hidden');
    });

    it('steps aside when another control (the selection bar) occupies its corner', () => {
        const body = renderOver(true, true);
        scrollTo(body, 900);
        const button = screen.getByRole('button', { name: 'Select photos' });
        expect(button.className).not.toContain('visible');
        expect(button.tabIndex).toBe(-1);
    });
});
