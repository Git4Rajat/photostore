import { get, post } from './apiClient';

// Estimated background progress after an upload (backend/processing_progress.py).
// The server owns the state; GET /api/processing is authoritative and a push only
// says "read it again" with the latest step attached, so a dropped message costs
// a slower bar, never a wrong one.

export type ProcessingStage = 'vision' | 'cluster' | 'index' | 'done';

export interface ProcessingWave {
    waveId: string;
    stage: ProcessingStage;
    state: string;
    startedAt: number;
    stageEnteredAt: number;
    done?: number;
    total?: number;
    etaSeconds?: number | null;
    processedSeconds?: number;
    failed?: number;
    // Running waves: photos already given up on, and what is wrong with the wave itself.
    failedMembers?: number;
    problem?: 'failed' | 'blocked' | 'stalled';
    noProgressSeconds?: number;
    blockedBy?: string;
}

export interface ProcessingPush {
    waveId: string;
    stage: ProcessingStage;
    step?: number | null;
    etaSeconds?: number | null;
}

const REQUEST = { singleAttempt: true, timeout: 15000 };

export const fetchProcessingWaves = async (): Promise<ProcessingWave[]> => {
    const body = await get<{ waves?: ProcessingWave[] }>('/api/processing', REQUEST);
    return Array.isArray(body?.waves) ? body.waves : [];
};

export const dismissProcessingWave = (waveId: string) => post(
    `/api/processing/${encodeURIComponent(waveId)}/dismiss`, {}, REQUEST);

// Time weights per stage until the server learns real ones: vision is the long pole.
const STAGE_ORDER: Array<'vision' | 'cluster' | 'index'> = ['vision', 'cluster', 'index'];
const STAGE_WEIGHT: Record<'vision' | 'cluster' | 'index', number> = { vision: 0.7, cluster: 0.15, index: 0.15 };

export const STAGE_TITLES: Record<ProcessingStage, string> = {
    vision: 'Reading your photos',
    cluster: 'Finding people',
    index: 'Getting search ready',
    done: 'All photos are ready',
};

// Overall fraction 0..1. Vision contributes its counted fraction; cluster and index have
// no count, so they contribute nothing until they finish.
export const overallFraction = (wave: ProcessingWave): number => {
    if (wave.stage === 'done') return 1;
    let fraction = 0;
    for (const stage of STAGE_ORDER) {
        if (stage === wave.stage) {
            if (stage === 'vision' && wave.total) fraction += STAGE_WEIGHT.vision * Math.min(1, (wave.done ?? 0) / wave.total);
            break;
        }
        fraction += STAGE_WEIGHT[stage];
    }
    return Math.min(0.99, fraction);
};

// The bar never goes backwards for a wave within a page's life.
export const clampMonotonic = (shown: Record<string, number>, wave: ProcessingWave): number => {
    const next = Math.max(shown[wave.waveId] ?? 0, overallFraction(wave));
    shown[wave.waveId] = next;
    return next;
};

export const stepNumber = (stage: ProcessingStage): number => (stage === 'done' ? 3 : STAGE_ORDER.indexOf(stage) + 1);

export const etaLabel = (seconds: number | null | undefined): string | null => {
    if (seconds === null || seconds === undefined || seconds < 0) return null;
    if (seconds <= 60) return 'less than a minute left';
    if (seconds <= 3600) return `about ${Math.round(seconds / 60)} min left`;
    return 'about an hour or more left';
};

// Past this long in one stage with a small fraction moving, say so rather than keep guessing.
export const TAKING_LONGER_FACTOR = 2;
export const takingLonger = (wave: ProcessingWave, now: number): boolean => {
    if (wave.stage === 'done' || wave.etaSeconds === null || wave.etaSeconds === undefined || wave.etaSeconds < 0) return false;
    return now / 1000 - wave.stageEnteredAt > Math.max(wave.etaSeconds, 60) * TAKING_LONGER_FACTOR;
};

// What the person is told when the wave is not simply moving. `failed` is the server giving up
// after its retries; `stalled` is no movement for a long while; `blocked` waits on an earlier upload.
export const problemText = (wave: ProcessingWave): string | null => {
    if (wave.stage === 'done') {
        return wave.failed ? `${wave.failed} ${wave.failed === 1 ? 'photo' : 'photos'} couldn’t be processed` : null;
    }
    if (wave.problem === 'failed') return `${STAGE_TITLES[wave.stage]} stopped after several tries`;
    if (wave.problem === 'blocked') return 'Waiting for an earlier upload to finish processing';
    if (wave.problem === 'stalled') return `Nothing has moved for ${Math.max(1, Math.round((wave.noProgressSeconds ?? 0) / 60))} min`;
    return null;
};
export const isTrouble = (wave: ProcessingWave): boolean => wave.problem === 'failed' || wave.problem === 'stalled'
    || (wave.stage === 'done' && Boolean(wave.failed));

// Tiny channel so the push socket owned by AppServicesProvider can reach the dock.
type Listener = () => void;
const listeners = new Set<Listener>();
let pushLive = false;
export const onProcessingSignal = (listener: Listener): (() => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
};
export const signalProcessing = (): void => listeners.forEach((listener) => listener());
export const setProcessingPushLive = (live: boolean): void => { pushLive = live; if (live) signalProcessing(); };
export const isProcessingPushLive = (): boolean => pushLive;
