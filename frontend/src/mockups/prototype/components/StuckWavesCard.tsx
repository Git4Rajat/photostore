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
    [key: string]: unknown;
}
interface Attention { waves: AttentionWave[]; truncated?: boolean }

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

    return (
        <div className="card-glass pt-recover-card">
            <strong>Stuck upload waves</strong>
            <span>List unfinished upload waves. A failed wave holds back every later wave from publishing; retry it here.</span>
            <button type="button" className="btn" disabled={!!busy} onClick={() => void refresh()}>{busy === 'list' ? 'Checking…' : 'Check'}</button>
            {waves && !waves.length && <span>No unfinished waves.</span>}
            {waves && waves.map((wave) => (
                <div key={String(wave.waveId)} className="pt-wave-row">
                    <span>{String(wave.waveId)}: {describeWave(wave)}</span>
                    {canRetry(wave) && (
                        <button type="button" className="btn mock-cta" disabled={!!busy} onClick={() => void retry(wave)}>
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
