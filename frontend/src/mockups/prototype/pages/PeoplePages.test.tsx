import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import type { Person } from '../types';

const mocks = vi.hoisted(() => ({ navigate: vi.fn(), mergePeopleBatch: vi.fn() }));

const people: Person[] = [
    { id: 'p1', name: null, swatch: 's1', photoIds: [], faceCount: 11 },
    { id: 'p2', name: 'Asha', swatch: 's2', photoIds: [], faceCount: 9 },
];

vi.mock('../store', () => ({
    useStore: () => ({
        people,
        peopleLoading: false,
        peopleTotal: people.length,
        peopleUnnamedTotal: 1,
        peopleHasMore: false,
        peopleUnavailable: false,
        loadMorePeople: vi.fn(),
        navigate: mocks.navigate,
        mergePeopleBatch: mocks.mergePeopleBatch,
        deletePeopleBatch: vi.fn(),
        fetchPeople: vi.fn(async () => {}),
    }),
}));
vi.mock('../../../services/imageClient', () => ({ useProtectedBlobUrls: () => ({}) }));
vi.mock('../../../services/backgroundRequestQueue', () => ({ enqueueBackgroundRequest: () => Promise.resolve() }));
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: vi.fn() }));

import { PeoplePage } from './PeoplePages';

const card = (text: string) => screen.getByText(text).closest('button') as HTMLButtonElement;

describe('PeoplePage long-press', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        mocks.navigate.mockReset();
        mocks.mergePeopleBatch.mockReset();
    });
    afterEach(() => vi.useRealTimers());

    it('starts selecting with the held person picked, without opening them on release', () => {
        render(<PeoplePage />);
        const held = card('Add name');
        fireEvent.touchStart(held);
        act(() => { vi.advanceTimersByTime(400); });
        fireEvent.touchEnd(held);
        fireEvent.click(held);

        expect(mocks.navigate).not.toHaveBeenCalled();
        expect(held.className).toContain('selected');
        expect(screen.getByRole('toolbar', { name: 'People selection actions' })).toBeTruthy();

        // Further taps add to the selection, and Merge is reachable once two are picked.
        fireEvent.click(card('Asha'));
        fireEvent.click(screen.getByRole('button', { name: 'Merge selected people' }));
        expect(mocks.mergePeopleBatch).toHaveBeenCalledWith('p2', ['p1']);
    });

    it('lets a scroll cancel the hold, so a tap still opens the person', () => {
        render(<PeoplePage />);
        const touched = card('Asha');
        fireEvent.touchStart(touched);
        fireEvent.touchMove(touched);
        act(() => { vi.advanceTimersByTime(400); });
        fireEvent.touchEnd(touched);
        fireEvent.click(touched);

        expect(mocks.navigate).toHaveBeenCalledWith('person', { personId: 'p2' });
        expect(screen.queryByRole('toolbar', { name: 'People selection actions' })).toBeNull();
    });
});
