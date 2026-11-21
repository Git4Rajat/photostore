import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertGlyph, CheckGlyph, CloseGlyph, ChevGlyph } from './RedesignIcons';
import { useAppServices } from '../../../components/AppServicesProvider';
import BottomSheet from './BottomSheet';
import { IconButton } from '../../../components/ui/Button';
import { StackWidget } from './WidgetStack';
import { plural } from '../../../utils/format';
import {
    STAGE_TITLES, clampMonotonic, dismissProcessingWave, etaLabel, fetchProcessingWaves, holdStalled, isProcessingPushLive, isTrouble, isWaiting,
    leadWave, onProcessingSignal, orderWaves, problemText, uploadLabel, retryOutcomeText, retryStuckProcessing, stepNumber, takingLonger, type ProcessingStage, type ProcessingWave,
} from '../../../services/processingProgress';

// Socket up: one safety read now and then. Socket down: follow the bar closely, but only while it is on screen.
const POLL_WITH_PUSH_MS = 2 * 60_000;
const POLL_WITHOUT_PUSH_MS = 20_000;
const RETRY_COOLDOWN_MS = 2 * 60_000;
// Browser and server clocks differ a little; a wave this close before the upload began still counts as its own.
const CLOCK_SLACK_S = 30;
const AFTER_UPLOAD_WATCH_MS = 15 * 60_000;
const STEPS: Array<'vision' | 'cluster' | 'index'> = ['vision', 'cluster', 'index'];
const STEP_COPY: Record<'vision' | 'cluster' | 'index', string> = {
    vision: 'Spotting places, things and text so they can be tagged',
    cluster: 'Matching the same faces across your photos',
    index: 'So you can find any photo by typing a few words',
};

const stepState = (wave: ProcessingWave, step: ProcessingStage): 'done' | 'now' | 'wait' => {
    if (wave.stage === 'done') return 'done';
    return stepNumber(step) < stepNumber(wave.stage) ? 'done' : step === wave.stage ? 'now' : 'wait';
};

const subline = (wave: ProcessingWave, now: number): string => {
    const problem = problemText(wave);
    if (wave.stage === 'done') {
        // Matches the "Upload from <time>" line: from the first photo to the wave finishing, not just the
        // processing after the upload closed (processedSeconds).
        const total = wave.startedAt > 0 && wave.stageEnteredAt > wave.startedAt ? wave.stageEnteredAt - wave.startedAt : (wave.processedSeconds ?? 0);
        const minutes = Math.max(1, Math.round(total / 60));
        const took = minutes >= 120 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : plural(minutes, 'minute');
        return [`Done in ${took}`, problem].filter(Boolean).join(' · ');
    }
    const base = `Step ${stepNumber(wave.stage)} of 3 · ${STAGE_TITLES[wave.stage]}`;
    if (wave.problem === 'blocked') return problem as string;
    if (wave.retriedRecently) return `${base} · retried, waiting for it to start`;
    if (problem) return `${base} · ${problem}`;
    if (takingLonger(wave, now)) return `${base} · taking longer than expected`;
    const eta = wave.stage === 'vision' ? etaLabel(wave.etaSeconds) : null;
    return eta ? `${base} · ${eta}` : base;
};

const headline = (wave: ProcessingWave): string => {
    if (wave.stage === 'done') return wave.failed ? 'Photos are ready, some had problems' : 'All photos are ready';
    if (wave.problem === 'failed') return 'Photo processing hit a problem';
    if (wave.problem === 'stalled') return 'Photo processing has stopped moving';
    return 'Getting your photos ready';
};

const ADVICE: Record<string, string> = {
    failed: 'We stopped after several tries. Your photos are safe in your library, but search and people may be incomplete until this is retried from Tools.',
    stalled: 'Your photos are safe. This is taking much longer than usual, and we are still watching it.',
    blocked: 'Your photos are safe. This upload is queued behind an earlier one and starts when that one is done.',
};

const stepList = (wave: ProcessingWave) => (
    <ol className="pd-steps">
        {STEPS.map((step, index) => {
            const state = stepState(wave, step);
            return (
                <li key={step} className={`pd-step ${state}`}>
                    <span className="pd-dot">{state === 'done' ? <CheckGlyph aria-hidden="true" /> : index + 1}</span>
                    <span className="pd-txt"><b>{STAGE_TITLES[step]}</b><span>{STEP_COPY[step]}</span></span>
                    <span className="pd-tag">{state === 'done' ? 'Done' : state === 'now' ? (wave.problem === 'failed' ? 'Stopped' : wave.problem === 'stalled' ? 'Stuck' : wave.problem === 'blocked' ? 'Waiting' : 'In progress') : ''}</span>
                </li>
            );
        })}
    </ol>
);

interface BlockProps { wave: ProcessingWave; full: boolean; onDismiss: (wave: ProcessingWave) => void }

// One upload in the sheet. Finished ones keep a single line with an X; waiting ones shrink to a
// line when there are many; only the running one shows its steps then.
const WaveBlock: React.FC<BlockProps> = ({ wave, full, onDismiss }) => {
    const finished = wave.stage === 'done';
    const waiting = isWaiting(wave);
    const count = wave.total ? plural(wave.total, 'photo') : '';
    return (
        <div className={`pd-wb${wave.problem === 'failed' ? ' bad' : ''}${waiting ? ' dim' : ''}`}>
            <div className="pd-wbh">
                <b>{uploadLabel(wave)}</b>{count ? <span>{count}</span> : null}
                {finished ? <button type="button" className="pd-wx" onClick={() => onDismiss(wave)} aria-label={`Dismiss ${uploadLabel(wave)}`}><CloseGlyph aria-hidden="true" /></button> : null}
            </div>
            {finished ? <p className="pd-line">{subline(wave, Date.now())}</p>
                : waiting && !full ? <p className="pd-line">Waiting for the earlier upload to finish</p>
                    : stepList(wave)}
            {!finished && wave.problem && wave.problem !== 'blocked' ? <p className="pd-line">{ADVICE[wave.problem]}</p> : null}
            {wave.failedMembers ? <p className="up-fine">{plural(wave.failedMembers, 'photo')} couldn’t be processed so far.</p> : null}
        </div>
    );
};

export const ProcessingDock: React.FC = () => {
    const { uploadActive } = useAppServices();
    const [waves, setWaves] = useState<ProcessingWave[]>([]);
    const [open, setOpen] = useState(false);
    const shown = useRef<Record<string, number>>({});
    // When the running upload began (seconds), to tell its own processing waves from earlier ones.
    const uploadStartedAt = useRef<number | null>(null);
    const retriedAt = useRef<Record<string, number>>({});
    const [, tick] = useState(0);
    const [retry, setRetry] = useState<{ busy: boolean; until: number; message: string }>({ busy: false, until: 0, message: '' });

    const load = useCallback(async () => {
        if (document.visibilityState !== 'visible' || !navigator.onLine) return;
        try {
            setWaves(await fetchProcessingWaves());
        } catch {
            // Advisory: keep what is on screen; the next read tries again.
        }
    }, []);

    useEffect(() => {
        void load();
        const off = onProcessingSignal(() => { void load(); });
        const visible = () => { if (document.visibilityState === 'visible') void load(); };
        document.addEventListener('visibilitychange', visible);
        return () => { off(); document.removeEventListener('visibilitychange', visible); };
    }, [load]);

    // An upload that ends (finished, paused, or cancelled) starts server work this page has not
    // heard about yet, and the push socket may be down: read right away, then keep looking for a while.
    const [watchUntil, setWatchUntil] = useState(0);
    const wasUploading = useRef(false);
    useEffect(() => {
        if (uploadActive) { wasUploading.current = true; return; }
        if (!wasUploading.current) return;
        wasUploading.current = false;
        setWatchUntil(Date.now() + AFTER_UPLOAD_WATCH_MS);
        void load();
    }, [uploadActive, load]);

    // The upload card speaks for the run that is going up, so processing waves that run started are not
    // repeated beside it. Older ones are separate activities and stay in the stack next to the upload.
    if (!uploadActive) uploadStartedAt.current = null;
    else if (uploadStartedAt.current === null) uploadStartedAt.current = Date.now() / 1000;
    const ownedByUpload = (candidate: ProcessingWave) => uploadStartedAt.current !== null && candidate.startedAt >= uploadStartedAt.current - CLOCK_SLACK_S;
    const ordered = orderWaves(waves.filter((candidate) => !ownedByUpload(candidate)).map((candidate) => holdStalled(candidate, retriedAt.current[candidate.waveId], Date.now())));
    const wave = leadWave(ordered);
    const unfinished = ordered.filter((candidate) => candidate.stage !== 'done');
    const finishedWaves = ordered.filter((candidate) => candidate.stage === 'done');
    const running = unfinished.length > 0;
    const watching = running || watchUntil > Date.now();
    useEffect(() => {
        if (!watching) return undefined;
        const timer = window.setInterval(() => {
            void load();
            tick((value) => value + 1); // "taking longer" is time-based
        }, isProcessingPushLive() ? POLL_WITH_PUSH_MS : POLL_WITHOUT_PUSH_MS);
        return () => window.clearInterval(timer);
    }, [watching, load]);

    if (!wave) return null;

    const fraction = clampMonotonic(shown.current, wave);
    const finished = wave.stage === 'done';
    const doRetry = async () => {
        setRetry({ busy: true, until: 0, message: '' });
        try {
            const result = await retryStuckProcessing();
            result.retried.forEach((item) => { retriedAt.current[item.waveId] = Date.now(); });
            setRetry({ busy: false, until: result.retried.length ? Date.now() + RETRY_COOLDOWN_MS : 0, message: retryOutcomeText(result) });
        } catch {
            setRetry({ busy: false, until: 0, message: 'Couldn’t retry just now. Try again in a moment.' });
        }
        void load();
    };
    const cooling = retry.until > Date.now();
    // Retry is one action for the whole library; offer it when any unfinished upload could use it.
    const canRetry = unfinished.some((candidate) => candidate.retryable);
    const askOwner = !canRetry && unfinished.some((candidate) => candidate.askOwner);
    const retryControl = canRetry ? (
        <button type="button" className="up-link pd-retry" onClick={() => void doRetry()} disabled={retry.busy || cooling}>
            {retry.busy ? 'Retrying…' : cooling ? 'Retry queued' : 'Retry'}
        </button>
    ) : null;
    const dismissWaves = (targets: ProcessingWave[]) => {
        const ids = new Set(targets.map((candidate) => candidate.waveId));
        setWaves((previous) => previous.filter((candidate) => !ids.has(candidate.waveId)));
        targets.forEach((target) => { void dismissProcessingWave(target.waveId).catch(() => undefined); });
    };
    const title = headline(wave);
    const trouble = isTrouble(wave);
    const sub = subline(wave, Date.now());
    const pct = Math.round(fraction * 100);
    const others = unfinished.filter((candidate) => candidate.waveId !== wave.waveId).length;
    const more = others && !finished
        ? `${plural(others, 'more upload')} ${others === 1 ? 'is' : 'are'} waiting${trouble ? '. Your photos are safe.' : ''}` : null;
    const full = ordered.length <= 2;

    return (
        <>
            <StackWidget id="processing" finished={finished} onDismiss={() => dismissWaves(finishedWaves)}>
            <div className={`up-dock up-bar pd-bar`} role="region" aria-label="Photo processing">
                <div className="pd-row">
                    <button type="button" className="up-head" onClick={() => setOpen(true)} aria-label={`${title}. ${sub}. ${more ?? ''} Show details`}>
                        <span className={`up-ico${trouble ? ' bad' : ''}`}>
                            {trouble ? <AlertGlyph aria-hidden="true" /> : finished ? <CheckGlyph aria-hidden="true" /> : wave.problem === 'blocked' ? <ChevGlyph aria-hidden="true" /> : <span className="pd-spin" aria-hidden="true" />}
                        </span>
                        <span className="up-t"><b>{title}</b><span>{sub}</span>{more ? <span className="pd-more">{more}</span> : null}</span>
                        {finished || wave.problem ? null : <span className="up-pct">{pct}%</span>}
                    </button>
                    <IconButton label="Details" variant="link" size="sm" onClick={() => setOpen(true)}>
                        <ChevGlyph style={{ transform: 'rotate(-90deg)' }} aria-hidden="true" />
                    </IconButton>
                    {finished ? <IconButton label="Dismiss" variant="link" size="sm" onClick={() => dismissWaves(finishedWaves)}><CloseGlyph aria-hidden="true" /></IconButton> : null}
                </div>
                {retryControl ? <div className="pd-actions">{retryControl}</div> : null}
                <div className={`pd-seg${trouble ? ' bad' : ''}${wave.problem ? ' idle' : ''}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label="Processing progress (estimated)">
                    {STEPS.map((step) => {
                        const state = stepState(wave, step);
                        const within = step === 'vision' && state === 'now' && wave.total ? Math.min(1, (wave.done ?? 0) / wave.total) : null;
                        return (
                            <i key={step} className={state}>
                                {state === 'done' ? null : <b style={within === null ? undefined : { width: `${Math.round(within * 100)}%` }} className={within === null && state === 'now' ? 'slide' : undefined} />}
                            </i>
                        );
                    })}
                </div>
            </div>
            </StackWidget>
            <BottomSheet open={open} onClose={() => setOpen(false)} title={title}>
                <div className="up-sheet">
                    <p className="up-lede">Your photos are safe. These steps make search and people work.</p>
                    <div className="pd-wlist">
                        {ordered.map((candidate) => (
                            <WaveBlock key={candidate.waveId} wave={candidate} full={full || candidate.waveId === wave.waveId} onDismiss={(target) => dismissWaves([target])} />
                        ))}
                    </div>
                    {finishedWaves.length > 1 ? <button type="button" className="up-link pd-clear" onClick={() => dismissWaves(finishedWaves)}>Clear finished</button> : null}
                    {canRetry ? <div className="up-btns">{retryControl}</div> : null}
                    {askOwner ? <p className="up-fine">Ask the library owner to retry this.</p> : null}
                    {retry.message ? <p className="up-fine" role="status">{retry.message}</p> : null}
                    {running ? <p className="up-fine">Estimated. You can close this page: this carries on by itself, and we'll tell you in Notifications when it's done.</p> : null}
                </div>
            </BottomSheet>
        </>
    );
};

export default ProcessingDock;
