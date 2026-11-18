import React, { useCallback, useEffect, useRef, useState } from 'react';
import { CheckGlyph, CloseGlyph, ChevGlyph } from './RedesignIcons';
import { useAppServices } from '../../../components/AppServicesProvider';
import BottomSheet from './BottomSheet';
import { plural } from '../../../utils/format';
import {
    STAGE_TITLES, clampMonotonic, dismissProcessingWave, etaLabel, fetchProcessingWaves, isProcessingPushLive,
    onProcessingSignal, stepNumber, takingLonger, type ProcessingStage, type ProcessingWave,
} from '../../../services/processingProgress';

// Socket up: one safety read now and then. Socket down: follow the bar closely, but only while it is on screen.
const POLL_WITH_PUSH_MS = 2 * 60_000;
const POLL_WITHOUT_PUSH_MS = 20_000;
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
    if (wave.stage === 'done') {
        const minutes = Math.max(1, Math.round((wave.processedSeconds ?? 0) / 60));
        return `Done in ${plural(minutes, 'minute')}`;
    }
    const base = `Step ${stepNumber(wave.stage)} of 3 · ${STAGE_TITLES[wave.stage]}`;
    if (takingLonger(wave, now)) return `${base} · taking longer than expected`;
    const eta = wave.stage === 'vision' ? etaLabel(wave.etaSeconds) : null;
    return eta ? `${base} · ${eta}` : base;
};

export const ProcessingDock: React.FC = () => {
    const { uploadActive } = useAppServices();
    const [waves, setWaves] = useState<ProcessingWave[]>([]);
    const [open, setOpen] = useState(false);
    const shown = useRef<Record<string, number>>({});
    const [, tick] = useState(0);

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

    const wave = waves.find((candidate) => candidate.stage !== 'done') ?? waves[0];
    const running = Boolean(wave && wave.stage !== 'done');
    const watching = running || watchUntil > Date.now();
    useEffect(() => {
        if (!watching) return undefined;
        const timer = window.setInterval(() => {
            void load();
            tick((value) => value + 1); // "taking longer" is time-based
        }, isProcessingPushLive() ? POLL_WITH_PUSH_MS : POLL_WITHOUT_PUSH_MS);
        return () => window.clearInterval(timer);
    }, [watching, load]);

    // The upload bar owns the screen while files are still going up.
    if (!wave || uploadActive) return null;

    const fraction = clampMonotonic(shown.current, wave);
    const finished = wave.stage === 'done';
    const dismiss = () => {
        setWaves((previous) => previous.filter((candidate) => candidate.waveId !== wave.waveId));
        void dismissProcessingWave(wave.waveId).catch(() => undefined);
    };
    const title = finished ? 'All photos are ready' : 'Getting your photos ready';
    const sub = subline(wave, Date.now());
    const pct = Math.round(fraction * 100);

    return (
        <>
            <div className="up-dock up-bar pd-bar" role="region" aria-label="Photo processing">
                <button type="button" className="up-head" onClick={() => setOpen(true)} aria-label={`${title}. ${sub}. Show details`}>
                    <span className="up-ico">{finished ? <CheckGlyph aria-hidden="true" /> : <span className="pd-spin" aria-hidden="true" />}</span>
                    <span className="up-t"><b>{title}</b><span>{sub}</span></span>
                    {finished ? null : <span className="up-pct">{pct}%</span>}
                    <ChevGlyph className="up-chev" aria-hidden="true" />
                </button>
                {finished ? <button type="button" className="pd-x" onClick={dismiss} aria-label="Dismiss"><CloseGlyph aria-hidden="true" /></button> : null}
                <div className="pd-seg" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label="Processing progress (estimated)">
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
            <BottomSheet open={open} onClose={() => setOpen(false)} title={title}>
                <div className="up-sheet">
                    <p className="up-lede">Your photos are safe. These steps make search and people work.</p>
                    <ol className="pd-steps">
                        {STEPS.map((step, index) => {
                            const state = stepState(wave, step);
                            return (
                                <li key={step} className={`pd-step ${state}`}>
                                    <span className="pd-dot">{state === 'done' ? <CheckGlyph aria-hidden="true" /> : index + 1}</span>
                                    <span className="pd-txt"><b>{STAGE_TITLES[step]}</b><span>{STEP_COPY[step]}</span></span>
                                    <span className="pd-tag">{state === 'done' ? 'Done' : state === 'now' ? 'In progress' : ''}</span>
                                </li>
                            );
                        })}
                    </ol>
                    {finished ? null : <p className="up-fine">Estimated. You can close this page: this carries on by itself, and we'll tell you in Notifications when it's done.</p>}
                    {wave.failed ? <p className="up-fine">{plural(wave.failed, 'photo')} couldn’t be processed.</p> : null}
                </div>
            </BottomSheet>
        </>
    );
};

export default ProcessingDock;
