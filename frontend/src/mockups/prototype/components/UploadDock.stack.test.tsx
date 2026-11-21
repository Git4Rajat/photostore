import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const services: Record<string, unknown> = {};
vi.mock('../../../components/AppServicesProvider', () => ({ useAppServices: () => services }));
const confirmDialog = vi.fn();
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: (o: unknown) => confirmDialog(o) }));

import UploadDock from './UploadDock';
import { WidgetStackProvider } from './WidgetStack';

const setServices = (over: Record<string, unknown>) => {
    Object.keys(services).forEach((k) => delete services[k]);
    Object.assign(services, {
        uploadActive: false, uploading: false, uploadProgress: null, uploadCheckProgress: null,
        pendingUploadSummary: null, pendingUploadFailedFiles: [],
        pauseActiveUpload: vi.fn(), cancelRemainingUpload: vi.fn(() => Promise.resolve()), stopActiveUpload: vi.fn(),
        retryPersistedUploadSession: vi.fn(() => new Promise<void>(() => undefined)),
        discardPersistedUploadSession: vi.fn(() => Promise.resolve()), requestUpload: vi.fn(),
    }, over);
};

const App: React.FC = () => <WidgetStackProvider><UploadDock /></WidgetStackProvider>;
const titles = () => Array.from(document.querySelectorAll('.wc-slot')).sort((a, b) => Number((a as HTMLElement).style.order) - Number((b as HTMLElement).style.order)).map((el) => el.querySelector('b')?.textContent);

// Ends a run on a "Cancelled" result card, the way a person would.
const cancelled = async () => {
    confirmDialog.mockResolvedValue(true);
    setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 0, totalCount: 5949, failedCount: 0 } });
    const view = render(<App />);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
    setServices({});
    await act(async () => { view.rerender(<App />); });
    return view;
};

describe('UploadDock in the widget stack', () => {
    beforeEach(() => {
        window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined })) as never;
        confirmDialog.mockReset();
    });

    it('a new upload becomes its own card in front of the cancelled result', async () => {
        const view = await cancelled();
        expect(titles()).toEqual(['Cancelled']);

        // Checking: the upload has not started yet.
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 0, totalCount: 40, failedCount: 0 }, uploadCheckProgress: { checked: 3, total: 40 } });
        await act(async () => { view.rerender(<App />); });
        expect(titles()).toEqual(['Checking 40 photos', 'Cancelled']);
        expect(document.querySelector('.wcnt')?.textContent).toBe('2');

        // Uploading.
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 5, totalCount: 40, failedCount: 0 } });
        await act(async () => { view.rerender(<App />); });
        expect(titles()).toEqual(['Uploading 40 items', 'Cancelled']);
        expect(document.querySelector('.wcnt')?.textContent).toBe('2');

        // Done: only the result is left.
        setServices({});
        await act(async () => { view.rerender(<App />); });
        expect(titles()).toEqual(['Cancelled']);
        expect(document.querySelector('.wcnt')).toBeNull();
    });

    it('Choose again, dismiss and Clear all act on the result only, never the running upload', async () => {
        const view = await cancelled();
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 5, totalCount: 40, failedCount: 0 } });
        await act(async () => { view.rerender(<App />); });

        fireEvent.click(document.querySelector('.wstk')!);
        expect(screen.getByText('2 activities')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Choose again' }));
        expect(services.requestUpload).toHaveBeenCalledTimes(1);
        // The result left; the running upload stays as a plain card.
        expect(screen.queryByText('Cancelled')).toBeNull();
        expect(titles()).toEqual(['Uploading 5 of 40']);
        expect(document.querySelector('.wcnt')).toBeNull();
    });

    it('Clear all removes the finished result and keeps the running upload', async () => {
        const view = await cancelled();
        setServices({ uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 5, totalCount: 40, failedCount: 0 } });
        await act(async () => { view.rerender(<App />); });
        fireEvent.click(document.querySelector('.wstk')!);
        fireEvent.click(screen.getByRole('button', { name: 'Clear all' }));
        expect(titles()).toEqual(['Uploading 5 of 40']);
    });
});
