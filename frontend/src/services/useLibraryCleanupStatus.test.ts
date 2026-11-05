import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { CleanupInfo } from './libraryClient';

const mocks = vi.hoisted(() => ({ getMine: vi.fn(), archive: vi.fn(), listener: null as null | ((event: { operation: string }) => void) }));
vi.mock('./libraryClient', () => ({ getMine: mocks.getMine, getLibraryCleanupInfo: mocks.archive }));
vi.mock('./libraryChanges', () => ({ subscribeLibraryChanges: (listener: typeof mocks.listener) => { mocks.listener = listener; return () => { mocks.listener = null; }; } }));
import { useLibraryCleanupStatus } from './useLibraryCleanupStatus';

const snapshot = (status = '', time = 0): CleanupInfo => ({
    lastCleanupStatus: status, lastCleanupTime: time,
    lastCleanupPhotosDeleted: 12, lastCleanupBlobsDeleted: 24, lastCleanupError: '',
});
beforeEach(() => {
    vi.useFakeTimers();
    mocks.getMine.mockReset().mockResolvedValue({ activeLibraryId: 'lib', cleanupInfo: snapshot() });
    mocks.archive.mockReset();
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('never wakes archive for idle Sharing, including repeated visible polling', async () => {
    renderHook(() => useLibraryCleanupStatus('lib', snapshot(), vi.fn()));
    await act(async () => { await vi.advanceTimersByTimeAsync(90000); });
    expect(mocks.getMine).toHaveBeenCalledTimes(3);
    expect(mocks.archive).not.toHaveBeenCalled();
});

it('detects cleanup on another device, reconciles on archive and stops at completion', async () => {
    const done = vi.fn();
    mocks.getMine.mockResolvedValue({ activeLibraryId: 'lib', cleanupInfo: snapshot('in_progress') });
    mocks.archive.mockResolvedValueOnce(snapshot('in_progress')).mockResolvedValueOnce(snapshot('completed', 100));
    renderHook(() => useLibraryCleanupStatus('lib', snapshot(), done));
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(mocks.archive).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(done).toHaveBeenCalledWith(snapshot('completed', 100));
    mocks.getMine.mockResolvedValue({ activeLibraryId: 'lib', cleanupInfo: snapshot('completed', 100) });
    await act(async () => { await vi.advanceTimersByTimeAsync(90000); });
    expect(mocks.archive).toHaveBeenCalledTimes(2);
    expect(done).toHaveBeenCalledTimes(1);
});

it('refreshes on cross-tab events and focus without archive calls when idle', async () => {
    renderHook(() => useLibraryCleanupStatus('lib', snapshot(), vi.fn()));
    await act(async () => { mocks.listener?.({ operation: 'library-cleanup' }); });
    await act(async () => { window.dispatchEvent(new Event('focus')); });
    expect(mocks.getMine).toHaveBeenCalledTimes(2);
    expect(mocks.archive).not.toHaveBeenCalled();
});

it('pauses in hidden tabs and resumes when visible', async () => {
    mocks.getMine.mockResolvedValue({ activeLibraryId: 'lib', cleanupInfo: snapshot('in_progress') });
    mocks.archive.mockResolvedValue(snapshot('in_progress'));
    const done = vi.fn();
    const view = renderHook(() => useLibraryCleanupStatus('lib', snapshot('in_progress'), done));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(mocks.archive).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(mocks.archive).toHaveBeenCalledTimes(1);
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(mocks.archive).toHaveBeenCalledTimes(2);
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(mocks.archive).toHaveBeenCalledTimes(2);
});

it('ignores stale library snapshots and does not follow their cleanup onto archive', async () => {
    mocks.getMine.mockResolvedValue({ activeLibraryId: 'other', cleanupInfo: snapshot('in_progress') });
    renderHook(() => useLibraryCleanupStatus('lib', snapshot(), vi.fn()));
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(mocks.archive).not.toHaveBeenCalled();
});

it('retries transient archive errors while active and stops after failure is reported', async () => {
    const done = vi.fn();
    mocks.archive.mockRejectedValueOnce(new Error('temporary')).mockResolvedValueOnce(snapshot('failed', 100));
    renderHook(() => useLibraryCleanupStatus('lib', snapshot('in_progress'), done));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(mocks.archive).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(mocks.archive).toHaveBeenCalledTimes(2);
    expect(done).not.toHaveBeenCalled();
});

it('does not report results from a request that finishes after unmount', async () => {
    const done = vi.fn();
    let resolve!: (info: CleanupInfo) => void;
    mocks.archive.mockReturnValue(new Promise<CleanupInfo>((finish) => { resolve = finish; }));
    const view = renderHook(() => useLibraryCleanupStatus('lib', snapshot('in_progress'), done));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    view.unmount();
    await act(async () => { resolve(snapshot('completed', 100)); });
    expect(done).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
});
