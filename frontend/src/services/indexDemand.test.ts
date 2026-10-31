import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import {
    __resetIndexDemandForTests, INDEX_ACTIVITY_WINDOW_MS, hasIndexDemand, hasRecentIndexActivity,
    isIndexDemandTab, noteIndexActivity, onIndexDemandReturn, setIndexDemandTab,
} from './indexDemand';

beforeEach(() => {
    vi.useFakeTimers();
    __resetIndexDemandForTests();
});
afterEach(() => { vi.useRealTimers(); __resetIndexDemandForTests(); });

it('has no demand until something happens', () => {
    expect(hasIndexDemand()).toBe(false);
    expect(hasRecentIndexActivity()).toBe(false);
    expect(isIndexDemandTab()).toBe(false);
});

it('grants demand on activity and lets it expire after the window', () => {
    noteIndexActivity();
    expect(hasIndexDemand()).toBe(true);
    vi.advanceTimersByTime(INDEX_ACTIVITY_WINDOW_MS - 1);
    expect(hasIndexDemand()).toBe(true);
    vi.advanceTimersByTime(1);
    expect(hasIndexDemand()).toBe(false);
});

it('grants demand while on the Tools or Sharing tab, with no decay', () => {
    setIndexDemandTab('tools');
    expect(hasIndexDemand()).toBe(true);
    vi.advanceTimersByTime(INDEX_ACTIVITY_WINDOW_MS * 10);
    expect(hasIndexDemand()).toBe(true);
    setIndexDemandTab('sharing');
    expect(hasIndexDemand()).toBe(true);
    setIndexDemandTab(null);
    expect(hasIndexDemand()).toBe(false);
});

it('notifies listeners only on a genuine off-to-on transition', () => {
    const resumed = vi.fn();
    const stop = onIndexDemandReturn(resumed);
    noteIndexActivity();
    expect(resumed).toHaveBeenCalledTimes(1);
    noteIndexActivity(); // already had demand -- no second notification
    expect(resumed).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(INDEX_ACTIVITY_WINDOW_MS + 1);
    expect(hasIndexDemand()).toBe(false);
    setIndexDemandTab('tools');
    expect(resumed).toHaveBeenCalledTimes(2);
    stop();
});
