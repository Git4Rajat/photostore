import React from 'react';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const services = vi.hoisted(() => ({ uploadActive: false }));
const api = vi.hoisted(() => ({ fetchProcessingWaves: vi.fn(), dismissProcessingWave: vi.fn(() => Promise.resolve()), retryStuckProcessing: vi.fn() }));

vi.mock('../../../components/AppServicesProvider', () => ({ useAppServices: () => services }));
vi.mock('../../../services/processingProgress', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../../../services/processingProgress')>()),
    fetchProcessingWaves: api.fetchProcessingWaves,
    dismissProcessingWave: api.dismissProcessingWave,
    retryStuckProcessing: api.retryStuckProcessing,
}));

import ProcessingDock from './ProcessingDock';

afterEach(() => { cleanup(); services.uploadActive = false; api.fetchProcessingWaves.mockReset(); api.retryStuckProcessing.mockReset(); });

const wave = (extra: object) => ({ waveId: 'w', stage: 'vision', state: 'processing', startedAt: 0, stageEnteredAt: Date.now() / 1000, ...extra });

describe('ProcessingDock', () => {
    it('shows the estimated step and percentage for a running wave', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ done: 50, total: 100, etaSeconds: 300 })]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Getting your photos ready')).toBeTruthy());
        expect(screen.getByText(/Step 1 of 3 · Reading your photos · about 5 min left/)).toBeTruthy();
        expect(screen.getByText('35%')).toBeTruthy();
    });

    it('says when it is finished and can be dismissed', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ stage: 'done', state: 'complete', processedSeconds: 240 })]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('All photos are ready')).toBeTruthy());
        screen.getByLabelText('Dismiss').click();
        await waitFor(() => expect(screen.queryByText('All photos are ready')).toBeNull());
        expect(api.dismissProcessingWave).toHaveBeenCalledWith('w');
    });

    it('reads again when an upload ends, so a cancelled upload shows its processing', async () => {
        services.uploadActive = true;
        api.fetchProcessingWaves.mockResolvedValue([]);
        const view = render(<ProcessingDock />);
        await waitFor(() => expect(api.fetchProcessingWaves).toHaveBeenCalledTimes(1));
        api.fetchProcessingWaves.mockResolvedValue([wave({ done: 0, total: 40 })]);
        services.uploadActive = false;
        view.rerender(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Getting your photos ready')).toBeTruthy());
        expect(api.fetchProcessingWaves).toHaveBeenCalledTimes(2);
    });

    it('says so when the server gave up on a stage instead of showing it as in progress', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ stage: 'cluster', state: 'ready', problem: 'failed', failedMembers: 4 })]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Photo processing hit a problem')).toBeTruthy());
        expect(screen.getByText(/Finding people stopped after several tries/)).toBeTruthy();
        expect(screen.queryByText('Getting your photos ready')).toBeNull();
        expect(screen.queryByText(/%$/)).toBeNull();
    });

    it('says when it is waiting on an earlier upload, and when nothing has moved', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ problem: 'blocked' })]);
        const view = render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Waiting for an earlier upload to finish processing')).toBeTruthy());
        view.unmount();
        api.fetchProcessingWaves.mockResolvedValue([wave({ problem: 'stalled', noProgressSeconds: 1500 })]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Photo processing has stopped moving')).toBeTruthy());
        expect(screen.getByText(/Nothing has moved for 25 min/)).toBeTruthy();
    });

    it('tells the person when a finished wave left photos unprocessed', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ stage: 'done', state: 'complete', processedSeconds: 600, failed: 56 })]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Photos are ready, some had problems')).toBeTruthy());
        expect(screen.getByText(/56 photos couldn’t be processed/)).toBeTruthy();
    });

    it('retries on click and then waits instead of letting the button be pressed again', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ stage: 'cluster', state: 'ready', problem: 'failed', retryable: true })]);
        api.retryStuckProcessing.mockResolvedValue({ retried: [{ waveId: 'w', stage: 'cluster' }], skipped: [] });
        render(<ProcessingDock />);
        const button = await screen.findByRole('button', { name: 'Retry' });
        button.click();
        await waitFor(() => expect(screen.getByRole('button', { name: 'Retry queued' })).toBeTruthy());
        expect((screen.getByRole('button', { name: 'Retry queued' }) as HTMLButtonElement).disabled).toBe(true);
        expect(api.retryStuckProcessing).toHaveBeenCalledTimes(1);
    });

    it('offers no Retry when nothing can be unblocked, and tells members to ask the owner', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ problem: 'blocked' })]);
        const view = render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Waiting for an earlier upload to finish processing')).toBeTruthy());
        expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
        view.unmount();
        api.fetchProcessingWaves.mockResolvedValue([wave({ problem: 'failed', askOwner: true })]);
        render(<ProcessingDock />);
        const head = await screen.findByRole('button', { name: /Show details/ });
        head.click();
        await waitFor(() => expect(screen.getByText('Ask the library owner to retry this.')).toBeTruthy());
        expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    });

    it('stays out of the way while files are still uploading', async () => {
        services.uploadActive = true;
        api.fetchProcessingWaves.mockResolvedValue([wave({ done: 1, total: 10 })]);
        render(<ProcessingDock />);
        await waitFor(() => expect(api.fetchProcessingWaves).toHaveBeenCalled());
        expect(screen.queryByText('Getting your photos ready')).toBeNull();
    });

    it('speaks for the running upload and says how many are waiting behind it', async () => {
        api.fetchProcessingWaves.mockResolvedValue([
            wave({ waveId: 'b', startedAt: 2, state: 'waiting_predecessor', done: 0, total: 30 }),
            wave({ waveId: 'a', startedAt: 1, done: 50, total: 100 }),
        ]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Getting your photos ready')).toBeTruthy());
        expect(screen.getByText('1 more upload is waiting')).toBeTruthy();
        expect(screen.getByText('35%')).toBeTruthy();
    });

    it('lets a problem win the bar and says the photos are safe', async () => {
        api.fetchProcessingWaves.mockResolvedValue([
            wave({ waveId: 'a', startedAt: 1, stage: 'cluster', state: 'ready', problem: 'failed', retryable: true }),
            wave({ waveId: 'b', startedAt: 2, state: 'waiting_predecessor', problem: 'blocked' }),
            wave({ waveId: 'c', startedAt: 3, state: 'waiting_predecessor', problem: 'blocked' }),
        ]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Photo processing hit a problem')).toBeTruthy());
        expect(screen.getByText('2 more uploads are waiting. Your photos are safe.')).toBeTruthy();
    });

    it('stops saying nothing has moved right after a retry', async () => {
        api.fetchProcessingWaves.mockResolvedValue([wave({ problem: 'stalled', noProgressSeconds: 1500, retryable: true })]);
        api.retryStuckProcessing.mockResolvedValue({ retried: [{ waveId: 'w', stage: 'vision' }], skipped: [] });
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('Photo processing has stopped moving')).toBeTruthy());
        screen.getByText('Retry').click();
        await waitFor(() => expect(screen.getByText('Getting your photos ready')).toBeTruthy());
        expect(screen.getByText(/retried, waiting for it to start/)).toBeTruthy();
    });

    it('clears all finished uploads at once from the sheet', async () => {
        api.fetchProcessingWaves.mockResolvedValue([
            wave({ waveId: 'a', startedAt: 1, stage: 'done', state: 'complete', processedSeconds: 60 }),
            wave({ waveId: 'b', startedAt: 2, stage: 'done', state: 'complete', processedSeconds: 60 }),
        ]);
        render(<ProcessingDock />);
        await waitFor(() => expect(screen.getByText('All photos are ready')).toBeTruthy());
        screen.getByRole('button', { name: /Show details/ }).click();
        await waitFor(() => expect(screen.getByText('Clear finished')).toBeTruthy());
        screen.getByText('Clear finished').click();
        await waitFor(() => expect(api.dismissProcessingWave).toHaveBeenCalledTimes(2));
    });
});
