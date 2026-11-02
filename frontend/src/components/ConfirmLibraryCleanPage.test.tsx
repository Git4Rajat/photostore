import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ status: vi.fn(), confirm: vi.fn() }));
vi.mock('../services/libraryClient', () => ({
    getLibraryCleanConfirmationStatus: mocks.status,
    confirmLibraryClean: mocks.confirm,
}));
vi.mock('./shared/AuthShell', () => ({ default: ({ children }: { children: React.ReactNode }) => <section>{children}</section> }));
import ConfirmLibraryCleanPage from './ConfirmLibraryCleanPage';

const mount = () => render(<MemoryRouter initialEntries={['/confirm-library-clean?token=example-token']}><ConfirmLibraryCleanPage /></MemoryRouter>);
const flush = async () => { await act(async () => { await Promise.resolve(); }); };
beforeEach(() => {
    vi.useFakeTimers();
    mocks.status.mockReset();
    mocks.confirm.mockReset().mockResolvedValue({ status: 'queued', jobId: 'job-1' });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('checks status before offering an explicit first confirmation', async () => {
    mocks.status.mockResolvedValue({ status: 'pending' });
    mount();
    expect(screen.getByText('Checking cleanup status…')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Confirm cleanup' })).toBeNull();
    await flush();
    expect(screen.getByRole('button', { name: 'Confirm cleanup' })).toBeTruthy();
    expect(mocks.confirm).not.toHaveBeenCalled();
});

it('restores running status after a reload without issuing another confirmation', async () => {
    mocks.status.mockResolvedValueOnce({ status: 'pending' }).mockResolvedValue({ status: 'queued', jobId: 'job-1' });
    const page = mount();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cleanup' }));
    await flush();
    expect(screen.getByText('Cleanup is queued…')).toBeTruthy();
    expect(mocks.confirm).toHaveBeenCalledTimes(1);
    page.unmount();
    mocks.status.mockResolvedValue({ status: 'running', jobId: 'job-1' });
    mount();
    await flush();
    expect(screen.getByText('Cleanup is running in the background…')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Confirm cleanup' })).toBeNull();
    expect(mocks.confirm).toHaveBeenCalledTimes(1);
});

it('shows completed counts when reopening a finished cleanup link', async () => {
    mocks.status.mockResolvedValue({ status: 'done', jobId: 'job-1', result: { photosDeleted: 12 } });
    mount();
    await flush();
    expect(screen.getByText('Cleanup complete — 12 photo(s)/video(s) removed.')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(mocks.status).toHaveBeenCalledTimes(1);
    expect(mocks.confirm).not.toHaveBeenCalled();
});

it('resumes waiting for another approver and follows the job to completion', async () => {
    mocks.status.mockResolvedValueOnce({ status: 'awaiting_more_approvals' })
        .mockResolvedValueOnce({ status: 'running', jobId: 'job-1' })
        .mockResolvedValue({ status: 'done', jobId: 'job-1' });
    mount();
    await flush();
    expect(screen.getByText(/Your confirmation was recorded/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Confirm cleanup' })).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByText('Cleanup is running in the background…')).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.getByText('Cleanup complete.')).toBeTruthy();
    expect(mocks.confirm).not.toHaveBeenCalled();
});

it('does not offer confirmation when status cannot be read', async () => {
    mocks.status.mockRejectedValue(new Error('This link has expired.'));
    mount();
    await flush();
    expect(screen.getByText('This link has expired.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Confirm cleanup' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeTruthy();
    expect(mocks.confirm).not.toHaveBeenCalled();
});

it('recovers a lost confirmation response using the saved job status', async () => {
    mocks.status.mockResolvedValueOnce({ status: 'pending' }).mockResolvedValue({ status: 'running', jobId: 'job-1' });
    mocks.confirm.mockRejectedValue(new Error('Connection lost'));
    mount();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cleanup' }));
    await flush();
    expect(screen.getByText('Cleanup is running in the background…')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Confirm cleanup' })).toBeNull();
    expect(mocks.confirm).toHaveBeenCalledTimes(1);
});

it('preserves an approval error when the server confirms that approval is still pending', async () => {
    mocks.status.mockResolvedValue({ status: 'pending' });
    mocks.confirm.mockRejectedValue(new Error('Could not save your approval.'));
    mount();
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cleanup' }));
    await flush();
    expect(screen.getByText('Could not save your approval.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Confirm cleanup' })).toBeTruthy();
});
