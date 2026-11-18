import React from 'react';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const services = vi.hoisted(() => ({ uploadActive: false }));
const api = vi.hoisted(() => ({ fetchProcessingWaves: vi.fn(), dismissProcessingWave: vi.fn(() => Promise.resolve()) }));

vi.mock('../../../components/AppServicesProvider', () => ({ useAppServices: () => services }));
vi.mock('../../../services/processingProgress', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../../../services/processingProgress')>()),
    fetchProcessingWaves: api.fetchProcessingWaves,
    dismissProcessingWave: api.dismissProcessingWave,
}));

import ProcessingDock from './ProcessingDock';

afterEach(() => { cleanup(); services.uploadActive = false; api.fetchProcessingWaves.mockReset(); });

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

    it('stays out of the way while files are still uploading', async () => {
        services.uploadActive = true;
        api.fetchProcessingWaves.mockResolvedValue([wave({ done: 1, total: 10 })]);
        render(<ProcessingDock />);
        await waitFor(() => expect(api.fetchProcessingWaves).toHaveBeenCalled());
        expect(screen.queryByText('Getting your photos ready')).toBeNull();
    });
});
