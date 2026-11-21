import React, { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { StackWidget, WidgetStackProvider } from './WidgetStack';

afterEach(cleanup);

const Card: React.FC<{ id: string; title: string; finished?: boolean; onDismiss?: () => void; onAction?: () => void }> = ({ id, title, finished, onDismiss, onAction }) => (
    <StackWidget id={id} finished={finished} onDismiss={onDismiss}>
        <div className="up-dock">
            <b>{title}</b>
            <button type="button" onClick={onAction}>{`Act ${id}`}</button>
            {finished ? <button type="button" onClick={onDismiss}>{`Dismiss ${id}`}</button> : null}
        </div>
    </StackWidget>
);

// Mounts cards one after another so "newest" is well defined.
const Demo: React.FC<{ actions?: Record<string, () => void> }> = ({ actions = {} }) => {
    const [gone, setGone] = useState<string[]>([]);
    const drop = (id: string) => () => setGone((g) => [...g, id]);
    return (
        <WidgetStackProvider>
            {!gone.includes('up') ? <Card id="up" title="Uploading" onAction={actions.up} /> : null}
            {!gone.includes('pr') ? <Card id="pr" title="Getting ready" onAction={actions.pr} /> : null}
            {!gone.includes('d1') ? <Card id="d1" title="Done one" finished onDismiss={drop('d1')} /> : null}
            {!gone.includes('d2') ? <Card id="d2" title="Done two" finished onDismiss={drop('d2')} /> : null}
        </WidgetStackProvider>
    );
};

describe('WidgetStack', () => {
    it('shows a single widget as a plain card without badge or header', () => {
        render(<WidgetStackProvider><Card id="a" title="Only one" /></WidgetStackProvider>);
        expect(screen.getByText('Only one')).toBeTruthy();
        expect(document.querySelector('.wcnt')).toBeNull();
        expect(document.querySelector('.wstk.col, .wstk.open')).toBeNull();
    });

    it('folds several into one stack with a count and at most three layers', () => {
        render(<Demo />);
        expect(document.querySelector('.wstk.col')).toBeTruthy();
        expect(document.querySelector('.wcnt')?.textContent).toBe('4');
        expect(document.querySelectorAll('.wc-slot:not(.gone)')).toHaveLength(3);
        expect(document.querySelectorAll('.wc-slot.gone')).toHaveLength(1);
        // The newest card is in front.
        expect(document.querySelector('.wc-slot:not([class*=" b"])')?.textContent).toContain('Done two');
    });

    it('opens on a tap without reaching the card, and shows a header and every card', () => {
        const act = vi.fn();
        render(<Demo actions={{ up: act }} />);
        fireEvent.click(screen.getByText('Act up'));
        expect(act).not.toHaveBeenCalled();
        expect(document.querySelector('.wstk.open')).toBeTruthy();
        expect(screen.getByText('4 activities')).toBeTruthy();
        expect(document.querySelectorAll('.wc-slot.gone')).toHaveLength(0);
        // Cards keep their own controls once open.
        fireEvent.click(screen.getByText('Act up'));
        expect(act).toHaveBeenCalledTimes(1);
    });

    it('folds back from the collapse button and from outside taps', () => {
        render(<Demo />);
        fireEvent.click(document.querySelector('.wstk')!);
        fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));
        expect(document.querySelector('.wstk.col')).toBeTruthy();
        fireEvent.click(document.querySelector('.wstk')!);
        fireEvent.click(document.querySelector('.wstk-scrim')!);
        expect(document.querySelector('.wstk.col')).toBeTruthy();
    });

    it('updates the count at once when a card is dismissed and stays open', () => {
        render(<Demo />);
        fireEvent.click(document.querySelector('.wstk')!);
        fireEvent.click(screen.getByText('Dismiss d1'));
        expect(screen.getByText('3 activities')).toBeTruthy();
        expect(document.querySelector('.wstk.open')).toBeTruthy();
    });

    it('Clear all removes only finished cards, and one left drops to a plain card', () => {
        render(<Demo />);
        fireEvent.click(document.querySelector('.wstk')!);
        fireEvent.click(screen.getByText('Clear all'));
        expect(screen.queryByText('Done one')).toBeNull();
        expect(screen.queryByText('Done two')).toBeNull();
        expect(screen.getByText('Uploading')).toBeTruthy();
        expect(screen.getByText('Getting ready')).toBeTruthy();
        expect(screen.getByText('2 activities')).toBeTruthy();
        // Nothing finished left: Clear all has nothing to do.
        expect((screen.getByText('Clear all') as HTMLButtonElement).disabled).toBe(true);
    });

    it('drops to a plain card when one is left', () => {
        const { rerender } = render(<WidgetStackProvider><Card id="a" title="A" /><Card id="b" title="B" finished onDismiss={() => undefined} /></WidgetStackProvider>);
        expect(document.querySelector('.wcnt')?.textContent).toBe('2');
        rerender(<WidgetStackProvider><Card id="a" title="A" /></WidgetStackProvider>);
        expect(document.querySelector('.wcnt')).toBeNull();
        expect(document.querySelector('.wstk.open, .wstk.col')).toBeNull();
        expect(screen.getByText('A')).toBeTruthy();
    });

    it('leaves toasts out of the stack', () => {
        render(<><Demo /><div className="pt-toasts"><div className="pt-toast">Skipped</div></div></>);
        expect(document.querySelector('.wcnt')?.textContent).toBe('4');
        expect(document.querySelector('.wstk')?.contains(screen.getByText('Skipped'))).toBe(false);
    });
});
