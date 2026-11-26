import { get, post, postUpload } from './apiClient';

// Estimated background progress after an upload (backend/processing_progress.py).
// The server owns the state; GET /api/processing is authoritative and a push only
// says "read it again" with the latest step attached, so a dropped message costs
// a slower bar, never a wrong one.

// 'upload' is an upload still going up, on this or another device; it is not a processing step.
export type ProcessingStage = 'upload' | 'vision' | 'cluster' | 'index' | 'done';

export interface ProcessingWave {
    waveId: string;
    stage: ProcessingStage;
    state: string;
    startedAt: number;
    stageEnteredAt: number;
    done?: number;
    total?: number;
    // Upload stage only: a sample of the photos already uploaded.
    files?: string[];
    // Upload stage only: the open upload sessions behind it, to tell this browser's own from another device's.
    sessionIds?: string[];
    etaSeconds?: number | null;
    processedSeconds?: number;
    failed?: number;
    // Running waves: photos already given up on, and what is wrong with the wave itself.
    failedMembers?: number;
    problem?: 'failed' | 'blocked' | 'stalled';
    noProgressSeconds?: number;
    blockedBy?: string;
    // A retry can unblock this wave (owner), or could if the library owner pressed it (member).
    retryable?: boolean;
    askOwner?: boolean;
    // Client-only: this wave was just retried, so an old "nothing has moved" reading no longer applies.
    retriedRecently?: boolean;
}

export interface RetryResult {
    retried: Array<{ waveId: string; stage: string }>;
    skipped: Array<{ waveId: string; reason: 'blocked' | 'working' | 'waiting' }>;
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

// One click: the server re-queues every stuck wave that is not itself waiting on an earlier one.
export const retryStuckProcessing = () => postUpload<RetryResult>('/upload/waves/live/retry-stuck', {});

export const retryOutcomeText = (result: RetryResult): string => {
    if (result.retried.length) return 'Retry queued. Later uploads will start once this one is done.';
    if (result.skipped.some((item) => item.reason === 'working')) return 'It is still being worked on. Give it a few minutes.';
    if (result.skipped.some((item) => item.reason === 'blocked')) return 'Waiting for an earlier upload, so there is nothing to retry yet.';
    return 'Nothing needed retrying.';
};

export const dismissProcessingWave = (waveId: string) => post(
    `/api/processing/${encodeURIComponent(waveId)}/dismiss`, {}, REQUEST);

// Time weights per stage until the server learns real ones: vision is the long pole.
const STAGE_ORDER: Array<'vision' | 'cluster' | 'index'> = ['vision', 'cluster', 'index'];
const STAGE_WEIGHT: Record<'vision' | 'cluster' | 'index', number> = { vision: 0.7, cluster: 0.15, index: 0.15 };

export const STAGE_TITLES: Record<ProcessingStage, string> = {
    upload: 'Uploading photos',
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

export const stepNumber = (stage: ProcessingStage): number => (stage === 'done' ? 3 : stage === 'upload' ? 0 : STAGE_ORDER.indexOf(stage) + 1);

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

// One block per upload: the ones still running oldest first (the oldest is the one everything
// else waits for), then finished ones, newest first.
export const orderWaves = (waves: ProcessingWave[]): ProcessingWave[] => [
    ...waves.filter((wave) => wave.stage !== 'done').sort((a, b) => a.startedAt - b.startedAt),
    ...waves.filter((wave) => wave.stage === 'done').sort((a, b) => b.startedAt - a.startedAt),
];

export const isWaiting = (wave: ProcessingWave): boolean => wave.stage !== 'done'
    && (wave.state === 'waiting_predecessor' || wave.problem === 'blocked');

// The wave the bar speaks for: a problem wins, then the one actually running, then the oldest
// unfinished, and only when everything is finished the latest finished one.
export const leadWave = (ordered: ProcessingWave[]): ProcessingWave | undefined => {
    const open = ordered.filter((wave) => wave.stage !== 'done');
    return open.find((wave) => wave.problem && wave.problem !== 'blocked')
        ?? open.find((wave) => !isWaiting(wave)) ?? open[0] ?? ordered[0];
};

export const uploadLabel = (wave: ProcessingWave): string => {
    const time = new Date(wave.startedAt * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `Upload from ${time}`;
};

// Retried a moment ago: the stuck reading is stale until a worker reports again.
export const STALL_HOLD_MS = 10 * 60_000;
export const holdStalled = (wave: ProcessingWave, retriedAt: number | undefined, now: number): ProcessingWave => (
    wave.problem === 'stalled' && retriedAt !== undefined && now - retriedAt < STALL_HOLD_MS
        ? { ...wave, problem: undefined, noProgressSeconds: undefined, retriedRecently: true }
        : wave);
