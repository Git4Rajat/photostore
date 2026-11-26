import React from 'react';
import { rememberOwnUploadSession } from '../../../services/ownUploadSessions';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const services = vi.hoisted(() => ({
    uploadActive: false, uploading: false, uploadProgress: null as unknown, uploadCheckProgress: null, pendingUploadSummary: null, pendingUploadFailedFiles: [] as unknown[],
    pauseActiveUpload: () => undefined, cancelRemainingUpload: () => Promise.resolve(), stopActiveUpload: () => undefined,
    retryPersistedUploadSession: () => Promise.resolve(), discardPersistedUploadSession: () => Promise.resolve(), requestUpload: () => undefined,
}));
const api = vi.hoisted(() => ({ fetchProcessingWaves: vi.fn(), dismissProcessingWave: vi.fn(() => Promise.resolve()), retryStuckProcessing: vi.fn() }));
vi.mock('../../../components/AppServicesProvider', () => ({ useAppServices: () => services }));
vi.mock('../../../services/processingProgress', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../../../services/processingProgress')>()),
    fetchProcessingWaves: api.fetchProcessingWaves,
    dismissProcessingWave: api.dismissProcessingWave,
}));

import UploadDock from './UploadDock';
import ProcessingDock from './ProcessingDock';
import { WidgetStackProvider } from './WidgetStack';

const wave = (extra: object) => ({ waveId: 'w', stage: 'vision', state: 'processing', startedAt: 0, stageEnteredAt: Date.now() / 1000, ...extra });
const App: React.FC = () => <WidgetStackProvider><UploadDock /><ProcessingDock /></WidgetStackProvider>;

describe('upload and processing in one stack', () => {
    beforeEach(() => {
        window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener: () => undefined, removeEventListener: () => undefined })) as never;
        Object.assign(services, { uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 5, totalCount: 40, failedCount: 0 } });
    });
    afterEach(() => { cleanup(); api.fetchProcessingWaves.mockReset(); });

    it('an older wave stays as its own card beside the running upload', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ waveId: 'old', done: 5, total: 20, startedAt: Date.now() / 1000 - 3600 })]);
        render(<App />);
        await waitFor(() => expect(screen.getByText('Getting your photos ready')).toBeTruthy());
        expect(screen.getByText('Uploading 40 items')).toBeTruthy();
        expect(document.querySelector('.wcnt')?.textContent).toBe('2');
    });

    it('the running upload\'s own wave is not shown as a second card', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ waveId: 'mine', done: 5, total: 20, startedAt: Date.now() / 1000 + 5 })]);
        render(<App />);
        await waitFor(() => expect(api.fetchProcessingWaves).toHaveBeenCalled());
        expect(screen.queryByText('Getting your photos ready')).toBeNull();
        expect(document.querySelector('.wcnt')).toBeNull();
    });

    it('another device\'s upload shows its count, but not on the device that is uploading', async () => {
        const upload = wave({ waveId: 'far', stage: 'upload', state: 'collecting', done: 40, total: 300, files: [] });
        Object.assign(services, { uploadActive: false, uploading: false, uploadProgress: null });
        api.fetchProcessingWaves.mockResolvedValue([upload]);
        render(<App />);
        await waitFor(() => expect(screen.getByText('Uploading from another device')).toBeTruthy());
        expect(screen.getByText('40 of 300 photos')).toBeTruthy();
        cleanup();
        Object.assign(services, { uploadActive: true, uploading: true, uploadProgress: { uploadedCount: 5, totalCount: 40, failedCount: 0 } });
        render(<App />);
        await waitFor(() => expect(api.fetchProcessingWaves).toHaveBeenCalledTimes(2));
        expect(screen.queryByText('Uploading from another device')).toBeNull();
    });

    it('a paused upload shows as paused, and this browser\'s own paused upload is not "another device"', async () => {
        Object.assign(services, { uploadActive: false, uploading: false, uploadProgress: null });
        api.fetchProcessingWaves.mockResolvedValue([wave({ waveId: 'far', stage: 'upload', state: 'paused', done: 40, total: 300, files: [], sessionIds: ['other'] })]);
        render(<App />);
        await waitFor(() => expect(screen.getByText('Upload paused on another device')).toBeTruthy());
        cleanup();
        rememberOwnUploadSession('mine');
        api.fetchProcessingWaves.mockResolvedValue([wave({ waveId: 'far', stage: 'upload', state: 'paused', done: 40, total: 300, files: [], sessionIds: ['mine'] })]);
        render(<App />);
        await waitFor(() => expect(api.fetchProcessingWaves).toHaveBeenCalledTimes(2));
        expect(screen.queryByText('Upload paused on another device')).toBeNull();
        expect(screen.queryByText('Uploading from another device')).toBeNull();
    });
});
