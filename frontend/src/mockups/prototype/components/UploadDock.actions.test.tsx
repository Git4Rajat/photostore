import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const services: Record<string, unknown> = {};
vi.mock('../../../components/AppServicesProvider', () => ({ useAppServices: () => services }));
const confirmDialog = vi.fn();
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: (o: unknown) => confirmDialog(o) }));

import UploadDock, { SKIP_UNDO_MS } from './UploadDock';

const failedFile = (n: number) => ({ key: `k${n}`, name: `IMG_${n}.HEIC`, size: 1, type: 'image/heic', lastModified: 0, uploadedBytes: 0, skippedDuplicate: false, status: 'failed', error: 'x' });

const setServices = (over: Record<string, unknown>) => {
    Object.keys(services).forEach((k) => delete services[k]);
    Object.assign(services, {
        uploadActive: false, uploading: false, uploadProgress: null, uploadCheckProgress: null,
        pendingUploadSummary: { fileCount: 2, failedCount: 2 }, pendingUploadFailedFiles: [failedFile(1), failedFile(2)],
        pauseActiveUpload: vi.fn(), cancelRemainingUpload: vi.fn(() => Promise.resolve()), stopActiveUpload: vi.fn(), retryPersistedUploadSession: vi.fn(() => new Promise<void>(() => undefined)),
        discardPersistedUploadSession: vi.fn(() => Promise.resolve()), requestUpload: vi.fn(),
    }, over);
};

describe('UploadDock actions', () => {
    beforeEach(() => {
        window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined })) as never;
        confirmDialog.mockReset();
        setServices({});
    });

    it('ignores a second tap on Try again and says what is happening', () => {
        render(<UploadDock />);
        const button = screen.getByRole('button', { name: 'Try again' });
        fireEvent.click(button);
        fireEvent.click(button);
        expect(services.retryPersistedUploadSession).toHaveBeenCalledTimes(1);
        const busy = screen.getByRole('button', { name: /Trying again…/ });
        expect((busy as HTMLButtonElement).disabled).toBe(true);
    });

    it('skip confirms at once and only drops the files after the undo window', () => {
        vi.useFakeTimers();
        render(<UploadDock />);
        fireEvent.click(screen.getByRole('button', { name: 'Skip these' }));
        expect(screen.getByText('2 items skipped')).toBeTruthy();
        expect(services.discardPersistedUploadSession).not.toHaveBeenCalled();
        act(() => { vi.advanceTimersByTime(SKIP_UNDO_MS); });
        expect(services.discardPersistedUploadSession).toHaveBeenCalledTimes(1);
        vi.useRealTimers();
    });

    it('undo keeps the files', () => {
        vi.useFakeTimers();
        render(<UploadDock />);
        fireEvent.click(screen.getByRole('button', { name: 'Skip these' }));
        fireEvent.click(screen.getByRole('button', { name: 'Undo' }));
        act(() => { vi.advanceTimersByTime(SKIP_UNDO_MS * 2); });
        expect(services.discardPersistedUploadSession).not.toHaveBeenCalled();
        expect(screen.getByText('2 items didn’t upload')).toBeTruthy();
        vi.useRealTimers();
    });

    it('pause disables both buttons straight away', () => {
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 3, totalCount: 10, failedCount: 0 }, pendingUploadFailedFiles: [] });
        render(<UploadDock />);
        fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
        expect(services.pauseActiveUpload).toHaveBeenCalledTimes(1);
        expect((screen.getByRole('button', { name: 'Pausing…' }) as HTMLButtonElement).disabled).toBe(true);
        expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(true);
    });

    it('cancel asks with the real numbers, then ends on a Cancelled result', async () => {
        confirmDialog.mockResolvedValue(true);
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 128, totalCount: 840, failedCount: 0 }, pendingUploadFailedFiles: [] });
        const view = render(<UploadDock />);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
        expect(confirmDialog).toHaveBeenCalledTimes(1);
        expect(confirmDialog.mock.calls[0][0]).toMatchObject({ message: expect.stringContaining('128 photos') });
        expect(screen.getByText('Cancelling upload')).toBeTruthy();
        setServices({ pendingUploadSummary: null, pendingUploadFailedFiles: [] });
        await act(async () => { view.rerender(<UploadDock />); });
        expect(screen.getByText('Cancelled')).toBeTruthy();
        expect(screen.getByText('128 added, 712 not')).toBeTruthy();
    });

    it('a declined cancel re-enables the buttons', async () => {
        confirmDialog.mockResolvedValue(false);
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 1, totalCount: 2, failedCount: 0 }, pendingUploadFailedFiles: [] });
        render(<UploadDock />);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
        expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
    });

    it('Pause and Cancel work again once a resumed upload is running', () => {
        const { rerender } = render(<UploadDock />);
        fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
        // The retry call lasts the whole upload, so it has not returned here.
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 1, totalCount: 5, failedCount: 0 }, pendingUploadFailedFiles: [] });
        rerender(<UploadDock />);
        expect((screen.getByRole('button', { name: 'Pause' }) as HTMLButtonElement).disabled).toBe(false);
        expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(false);
        fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
        expect(services.pauseActiveUpload).toHaveBeenCalledTimes(1);
    });

    it('resuming a plain pause reads as a continuation, not a retry', () => {
        setServices({ pendingUploadSummary: { fileCount: 280, failedCount: 0 }, pendingUploadFailedFiles: [] });
        const { rerender } = render(<UploadDock />);
        fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
        expect(screen.queryByText(/Trying again|retried/)).toBeNull();
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 120, totalCount: 400, failedCount: 0 }, pendingUploadFailedFiles: [] });
        rerender(<UploadDock />);
        expect(screen.getByText('Resuming upload…')).toBeTruthy();
        expect(screen.getByText(/Picking up where you left off · 280 files left/)).toBeTruthy();
        expect(screen.queryByText(/retried/)).toBeNull();
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 135, totalCount: 400, failedCount: 0 }, pendingUploadFailedFiles: [] });
        rerender(<UploadDock />);
        expect(screen.getByText('Uploading 135 of 400')).toBeTruthy();
        expect(screen.getByText('135 added · 265 waiting')).toBeTruthy();
    });
});
