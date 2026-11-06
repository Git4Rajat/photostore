import React, { useRef } from 'react';
import { describe, expect, it } from 'vitest';
import { act, fireEvent, render } from '@testing-library/react';
import { useCollapseOnScroll } from './useCollapseOnScroll';

const Probe: React.FC = () => {
    const ref = useRef<HTMLDivElement | null>(null);
    const collapsed = useCollapseOnScroll(ref);
    return <div><div data-testid="body" ref={ref} /><span data-testid="state">{collapsed ? 'collapsed' : 'shown'}</span></div>;
};

const scrollTo = (el: HTMLElement, top: number) => {
    Object.defineProperty(el, 'scrollTop', { value: top, writable: true, configurable: true });
    act(() => { fireEvent.scroll(el); });
};

describe('useCollapseOnScroll', () => {
    it('collapses on a downward scroll past the threshold and returns on an upward one', () => {
        const { getByTestId } = render(<Probe />);
        const body = getByTestId('body');
        const state = () => getByTestId('state').textContent;
        expect(state()).toBe('shown');
        scrollTo(body, 300);
        expect(state()).toBe('collapsed');
        scrollTo(body, 280);
        expect(state()).toBe('shown');
    });

    it('stays shown near the top and ignores sub-step jitter', () => {
        const { getByTestId } = render(<Probe />);
        const body = getByTestId('body');
        scrollTo(body, 60);
        expect(getByTestId('state').textContent).toBe('shown');
        scrollTo(body, 300);
        expect(getByTestId('state').textContent).toBe('collapsed');
        scrollTo(body, 297);
        expect(getByTestId('state').textContent).toBe('collapsed');
        scrollTo(body, 40);
        expect(getByTestId('state').textContent).toBe('shown');
    });
});
