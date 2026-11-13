import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Toast } from '../types';

const store = vi.hoisted(() => ({ toasts: [] as Toast[], dismissToast: vi.fn() }));
vi.mock('../store', () => ({ useStore: () => store }));

import Toasts from './Toasts';
import { EXIT_MS } from '../../../services/swipeDismiss';
import { gesture } from '../../../test/pointerGesture';

describe('Toasts', () => {
    beforeEach(() => { vi.useFakeTimers(); store.dismissToast.mockReset(); });
    afterEach(() => { cleanup(); vi.useRealTimers(); });

    it('closes the toast that was tapped or swiped away', () => {
        store.toasts = [{ id: 't1', message: 'Saved' }, { id: 't2', message: 'Moved' }];
        render(<Toasts />);
        act(() => { gesture(screen.getByText('Saved').parentElement!, 0, 0, 100); });
        act(() => { vi.advanceTimersByTime(EXIT_MS); });
        expect(store.dismissToast).toHaveBeenCalledWith('t1');
        act(() => { gesture(screen.getByText('Moved').parentElement!, -100, 0, 500); });
        act(() => { vi.advanceTimersByTime(EXIT_MS); });
        expect(store.dismissToast).toHaveBeenLastCalledWith('t2');
    });

    it('still runs its action from the button, once', () => {
        const onAction = vi.fn();
        store.toasts = [{ id: 't3', message: 'Deleted 2 items', actionLabel: 'Undo', onAction }];
        render(<Toasts />);
        const undo = screen.getByRole('button', { name: 'Undo' });
        act(() => { gesture(undo.parentElement!, 0, 0, 100, undo); });
        fireEvent.click(undo);
        act(() => { vi.advanceTimersByTime(EXIT_MS * 3); });
        expect(onAction).toHaveBeenCalledTimes(1);
        expect(store.dismissToast).toHaveBeenCalledTimes(1);
    });
});
