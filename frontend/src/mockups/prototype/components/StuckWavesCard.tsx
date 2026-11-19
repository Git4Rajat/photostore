import React, { useState } from 'react';
import { getUpload, postUpload } from '../../../services/apiClient';
import { failureReason } from '../toolsActions';

interface WaveHealth {
    state?: string;
    reason?: string;
    stage?: string | null;
    dispatchAttempts?: number;
    ownerActive?: boolean;
    noProgressSeconds?: number;
    blockedByWaveId?: string;
}
interface AttentionWave {
    waveId?: string;
    state: string;
    health?: WaveHealth;
    /** Set on a finished wave whose photos gave up processing and have not been retried yet. */
    failedMembers?: number;
    [key: string]: unknown;
}
interface Attention { waves: AttentionWave[]; truncated?: boolean }
interface RetryFailedResult { retried: number; skipped: number; failedRetries: number; after: string }

export const failedPhotoCount = (wave: AttentionWave): number => (wave.state === 'complete' ? wave.failedMembers ?? 0 : 0);

// A wave can be retried when its current stage gave up or was never queued, or when no worker owns it and it
// has made no progress for a while (e.g. attempts used up but nothing queued). The server also refuses the
// retry while a worker still owns the stage.
export const IDLE_RETRY_SECONDS = 600;
export const canRetry = (wave: AttentionWave): boolean => {
    const health = wave.health;
    if (!health?.stage) return false;
    if (health.state === 'failed' || health.reason === 'dispatch_missing') return true;
    return health.state === 'pending' && health.ownerActive === false
        && (health.noProgressSeconds ?? 0) >= IDLE_RETRY_SECONDS;
};

export const describeWave = (wave: AttentionWave): string => {
    const failed = failedPhotoCount(wave);
    if (failed) return `Finished, but ${failed} ${failed === 1 ? 'photo' : 'photos'} failed processing`;
    const health = wave.health || {};
    if (health.state === 'failed') return `Failed in ${health.stage || 'a stage'} after ${health.dispatchAttempts ?? 0} attempts`;
    if (health.state === 'blocked') return `Waiting for an earlier wave (${health.blockedByWaveId || 'unknown'}) to publish`;
    if (health.reason === 'dispatch_missing') return `Not queued for ${health.stage || 'its stage'}`;
    if (canRetry(wave)) return `Idle in ${health.stage} for ${Math.round((health.noProgressSeconds ?? 0) / 60)} min with no worker`;
    if (health.state === 'processing') return `Running ${health.stage || ''}`.trim();
    return `Queued for ${health.stage || 'its next stage'}`;
};

const StuckWavesCard: React.FC<{ onMessage: (message: string, error?: boolean) => void }> = ({ onMessage }) => {
    const [waves, setWaves] = useState<AttentionWave[] | null>(null);
    const [truncated, setTruncated] = useState(false);
    const [busy, setBusy] = useState('');

    const refresh = async () => {
        setBusy('list');
        try {
            const result = await getUpload<Attention>('/upload/waves/live/attention');
            setWaves(result.waves || []);
            setTruncated(!!result.truncated);
        } catch (error) {
            onMessage(failureReason(error, 'Couldn’t check upload waves'), true);
        } finally {
            setBusy('');
        }
    };

    const retry = async (wave: AttentionWave) => {
        const id = String(wave.waveId || '');
        if (!id) return;
        setBusy(id);
        try {
            await postUpload(`/upload/waves/live/${encodeURIComponent(id)}/repair`, { action: 'retry-stage' });
            onMessage('Retry queued. Later waves will publish once this one completes.');
            await refresh();
        } catch (error) {
            onMessage(failureReason(error, 'Couldn’t retry this wave'), true);
            setBusy('');
        }
    };

    // Pages through the wave's failed photos; each page starts a fresh processing run for those still on the
    // generation that failed (photos since deleted or already reprocessed are skipped by the server).
    const retryFailed = async (wave: AttentionWave) => {
        const id = String(wave.waveId || '');
        if (!id) return;
        setBusy(id);
        let retried = 0;
        let skipped = 0;
        let failed = 0;
        try {
            let after = '';
            do {
                const result = await postUpload<RetryFailedResult>(`/upload/waves/live/${encodeURIComponent(id)}/repair`,
                    { action: 'retry-failed', after });
                retried += result.retried;
                skipped += result.skipped;
                failed += result.failedRetries;
                after = result.after;
            } while (after);
            onMessage(failed
                ? `Retried ${retried} photos; ${failed} couldn’t be queued. Try again later.`
                : `Retrying ${retried} ${retried === 1 ? 'photo' : 'photos'}${skipped ? ` (${skipped} already handled)` : ''}.`, failed > 0);
            await refresh();
        } catch (error) {
            onMessage(failureReason(error, 'Couldn’t retry the failed photos'), true);
            setBusy('');
        }
    };

    return (
        <div className="card-glass pt-recover-card">
            <strong>Stuck upload waves</strong>
            <span>List unfinished upload waves, and finished ones with photos that failed processing. A failed wave holds back every later wave from publishing; retry it here.</span>
            <button type="button" className="btn" disabled={!!busy} onClick={() => void refresh()}>{busy === 'list' ? 'Checking…' : 'Check'}</button>
            {waves && !waves.length && <span>Nothing needs attention.</span>}
            {waves && waves.map((wave) => (
                <div key={String(wave.waveId)} className="pt-wave-row">
                    <span>{String(wave.waveId)}: {describeWave(wave)}</span>
                    {failedPhotoCount(wave) > 0 && (
                        <button type="button" className="btn btn-primary" disabled={!!busy} onClick={() => void retryFailed(wave)}>
                            {busy === wave.waveId ? 'Retrying…' : `Retry ${failedPhotoCount(wave)} failed`}
                        </button>
                    )}
                    {canRetry(wave) && (
                        <button type="button" className="btn btn-primary" disabled={!!busy} onClick={() => void retry(wave)}>
                            {busy === wave.waveId ? 'Retrying…' : 'Retry'}
                        </button>
                    )}
                </div>
            ))}
            {truncated && <span>Showing the first 25 unfinished waves.</span>}
        </div>
    );
};

export default StuckWavesCard;
