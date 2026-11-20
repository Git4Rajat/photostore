import React, { useEffect, useRef, useState } from 'react';
import { ChevGlyph as ChevronRight, AlertGlyph as CircleAlert, UploadGlyph as CloudUpload, PhotosGlyph as PhotoIcon, InfoGlyph as Info, CloseGlyph as CloseIcon, CheckGlyph } from './RedesignIcons';
import { useAppServices } from '../../../components/AppServicesProvider';
import { Button, IconButton } from '../../../components/ui/Button';
import { confirmDialog } from '../../../components/shared/dialogs';
import { plural } from '../../../utils/format';
import type { PersistedUploadFile } from '../../../types/browserProcessing';
import BottomSheet from './BottomSheet';
import { plainFailureReason, timeLeftLabel } from './uploadDockText';

const FAILURES_SHOWN = 4;

const useNarrow = (): boolean => {
    const query = '(max-width: 805px)';
    const [narrow, setNarrow] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia(query).matches);
    useEffect(() => {
        if (typeof window.matchMedia !== 'function') return undefined;
        const mq = window.matchMedia(query);
        const on = () => setNarrow(mq.matches);
        on();
        mq.addEventListener('change', on);
        return () => mq.removeEventListener('change', on);
    }, []);
    return narrow;
};

const Progress: React.FC<{ pct?: number; bad?: boolean }> = ({ pct, bad }) => (
    <div
        className={`up-prog${bad ? ' bad' : ''}${pct === undefined ? ' ind' : ''}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-label="Upload progress"
    >
        <i style={pct === undefined ? undefined : { width: `${pct}%` }} />
    </div>
);

// How long "Skip" can be undone before the failed files are really dropped.
export const SKIP_UNDO_MS = 6000;

type Acting = 'pause' | 'cancel' | 'retry' | null;
type Outcome = { kind: 'cancelled'; added: number; notAdded: number };

/** The one-line result that replaces the dock after cancel / skip, with a single action and an optional dismiss. */
const ResultBar: React.FC<{
    narrow: boolean;
    bad?: boolean;
    title: string;
    sub: string;
    action?: { label: string; onClick: () => void; disabled?: boolean };
    onDismiss?: () => void;
}> = ({ narrow, bad, title, sub, action, onDismiss }) => (
    <div className={`up-dock up-result ${narrow ? 'up-bar' : 'up-tray'}`} role="status" aria-live="polite">
        <div className="up-rr">
            <span className={`up-ico${bad ? ' bad' : ''}`}>{bad ? <CircleAlert aria-hidden="true" /> : <CheckGlyph aria-hidden="true" />}</span>
            <span className="up-t"><b>{title}</b><span>{sub}</span></span>
            {action ? <button type="button" className="up-link" onClick={action.onClick} disabled={action.disabled}>{action.label}</button> : null}
            {onDismiss ? <IconButton label="Dismiss" variant="link" size="sm" onClick={onDismiss}><CloseIcon aria-hidden="true" /></IconButton> : null}
        </div>
    </div>
);

const FailedRows: React.FC<{ files: PersistedUploadFile[]; onRetry?: () => void; retryDisabled?: boolean }> = ({ files, onRetry, retryDisabled }) => {
    const [all, setAll] = useState(false);
    const shown = all ? files : files.slice(0, FAILURES_SHOWN);
    return (
        <ul className="up-list">
            {shown.map((file, index) => (
                <li key={file.key} className="up-row">
                    {file.previewDataUrl
                        ? <img src={file.previewDataUrl} alt="" className="up-th" />
                        : <span className="up-th up-th-empty"><PhotoIcon aria-hidden="true" /></span>}
                    <span className="up-rt">
                        <b>{file.name}</b>
                        <span className="why">{plainFailureReason(file.error)}</span>
                    </span>
                    {onRetry && index === 0 ? <button type="button" className="up-link" onClick={onRetry} disabled={retryDisabled}>Retry</button> : null}
                </li>
            ))}
            {!all && files.length > FAILURES_SHOWN ? (
                <li className="up-row up-more">
                    <button type="button" className="up-link mut" onClick={() => setAll(true)}>Show {files.length - FAILURES_SHOWN} more</button>
                </li>
            ) : null}
        </ul>
    );
};

export const UploadDock: React.FC = () => {
    const {
        uploadActive, uploading, uploadProgress: progress, uploadCheckProgress: checking, pendingUploadSummary, pendingUploadFailedFiles,
        pauseActiveUpload, cancelRemainingUpload, retryPersistedUploadSession, discardPersistedUploadSession, requestUpload,
    } = useAppServices();
    const narrow = useNarrow();
    const [sheetOpen, setSheetOpen] = useState(false);
    const [collapsed, setCollapsed] = useState(false);
    const [hidden, setHidden] = useState(false);
    // The action the user just tapped. Set synchronously on the click so a second tap is ignored
    // and the button already says what is happening, before the services have caught up.
    const [acting, setActing] = useState<Acting>(null);
    const [cancelling, setCancelling] = useState(false);
    const [outcome, setOutcome] = useState<Outcome | null>(null);
    const [skipped, setSkipped] = useState<{ count: number } | null>(null);
    // How many files the last "Try again" started with, so the result can say how many worked.
    const [retryOf, setRetryOf] = useState<number | null>(null);
    // Set when the person resumes a plain pause (nothing failed). Resuming is a continuation, so it keeps the
    // normal upload wording; `resumeBase` is the added count the run picked up from, to tell when it has moved.
    const [resumed, setResumed] = useState(false);
    const [resumeBase, setResumeBase] = useState<number | null>(null);
    const startRef = useRef<{ at: number; base: number } | null>(null);
    const cancelSnapshotRef = useRef({ added: 0, notAdded: 0 });
    const discardingRef = useRef(false);
    const skipTimerRef = useRef<number | null>(null);

    const added = progress?.uploadedCount ?? 0;
    const total = progress?.totalCount ?? 0;
    const failed = progress?.failedCount ?? 0;
    const pending = !uploadActive && (pendingUploadSummary?.fileCount ?? 0) > 0;
    const failedFiles = pendingUploadFailedFiles;
    const showFailed = pending && failedFiles.length > 0;
    const visible = cancelling || skipped || outcome ? true : uploadActive ? !hidden : pending;

    useEffect(() => {
        if (!uploadActive) { startRef.current = null; setHidden(false); return; }
        if (!startRef.current) startRef.current = { at: Date.now(), base: added };
        setOutcome(null);
    }, [uploadActive, added]);

    // A pause has landed once the upload is no longer active.
    useEffect(() => { if (acting === 'pause' && !uploadActive) setActing(null); }, [acting, uploadActive]);

    useEffect(() => {
        if (!resumed) return;
        if (!uploadActive) { if (acting !== 'retry') { setResumed(false); setResumeBase(null); } return; }
        if (resumeBase === null) setResumeBase(added);
    }, [resumed, uploadActive, acting, added, resumeBase]);

    // Resume has started once the upload is active again; from then on Pause and Cancel are available.
    useEffect(() => { if (acting === 'retry' && uploadActive) setActing(null); }, [acting, uploadActive]);

    // Everything is finished or dropped: forget the last retry.
    useEffect(() => { if (!pending && !uploadActive) setRetryOf(null); }, [pending, uploadActive]);

    // Cancel = pause (files in flight finish and form their wave), then drop what never started once the
    // pause has landed; files that failed stay for Try again. Then say how it ended.
    useEffect(() => {
        if (!cancelling || uploadActive || discardingRef.current) return;
        discardingRef.current = true;
        void (async () => {
            if (pendingUploadSummary) await cancelRemainingUpload();
            discardingRef.current = false;
            setCancelling(false);
            setActing(null);
            setSheetOpen(false);
            setOutcome({ kind: 'cancelled', ...cancelSnapshotRef.current });
        })();
    }, [cancelling, uploadActive, pendingUploadSummary, cancelRemainingUpload]);

    useEffect(() => { if (!visible) setSheetOpen(false); }, [visible]);

    // Leaving with a skip pending drops nothing: the files stay in the retry list.
    useEffect(() => () => { if (skipTimerRef.current !== null) window.clearTimeout(skipTimerRef.current); }, []);

    if (!visible) return null;

    const duplicates = progress?.skippedDuplicateCount ?? 0;
    const pct = checking
        ? (checking.total ? Math.round((checking.checked / checking.total) * 100) : 0)
        : total ? Math.round((added / total) * 100) : 0;
    const waiting = Math.max(0, total - added - failed);
    const timeLeft = startRef.current
        ? timeLeftLabel(Date.now() - startRef.current.at, added - startRef.current.base, waiting)
        : null;
    const duplicatesLabel = duplicates > 0 ? `${duplicates} already in your library` : null;
    const retrying = retryOf !== null && uploadActive;
    const resuming = resumed && uploadActive && !checking && (resumeBase === null || added <= resumeBase);
    const label = uploading ? 'Uploading' : 'Finishing upload';
    const worked = retryOf === null ? 0 : Math.max(0, retryOf - failedFiles.length);
    const title = cancelling
        ? 'Cancelling upload'
        : resuming
        ? 'Resuming upload…'
        : checking && uploadActive
        ? `Checking ${plural(checking.total, 'photo')}`
        : retrying
        ? 'Trying again'
        : !uploadActive
        ? (showFailed ? `${plural(failedFiles.length, retryOf === null ? 'item' : 'item')} ${retryOf === null ? 'didn’t' : 'still didn’t'} upload` : 'Upload paused')
        : total ? `${label} ${added} of ${total}` : `${label}…`;
    const sub = cancelling
        ? 'Photos already added stay in your library'
        : !uploadActive
        ? (showFailed ? (retryOf === null ? 'Tap to see why' : `${worked} of ${retryOf} worked this time`) : `${plural(pendingUploadSummary?.fileCount ?? 0, 'file')} waiting`)
        : checking ? 'Looking for ones you already have'
        : resuming ? 'Picking up where you left off'
        : [timeLeft ?? (progress?.mbPerSecond ? `${progress.mbPerSecond.toFixed(1)} MB/s` : 'Starting…'), duplicatesLabel].filter(Boolean).join(' · ');

    const heading = cancelling || retrying || resumed ? title : uploadActive ? (!checking && uploading && total ? `Uploading ${plural(total, 'item')}` : title) : title;

    const pause = () => {
        if (acting) return;
        setActing('pause');
        pauseActiveUpload();
    };
    const cancel = async () => {
        if (acting) return;
        setActing('cancel');
        const stays = added;
        const notAdded = Math.max(0, total - added);
        const ok = await confirmDialog({
            title: 'Cancel this upload?',
            message: `The ${plural(stays, 'photo')} already added stay in your library. The other ${notAdded} won’t be uploaded.`,
            confirmLabel: 'Cancel upload',
            cancelLabel: 'Keep uploading',
            danger: true,
        });
        if (!ok) { setActing(null); return; }
        cancelSnapshotRef.current = { added: stays, notAdded };
        setCancelling(true);
        if (narrow) setSheetOpen(true);
        pauseActiveUpload();
    };
    const retry = async () => {
        if (acting) return;
        setActing('retry');
        if (showFailed) setRetryOf(failedFiles.length);
        else { setResumed(true); setResumeBase(null); }
        try {
            await retryPersistedUploadSession();
        } finally {
            // The call above lasts the whole resumed upload; Pause or Cancel tapped meanwhile must keep their state.
            setActing(a => (a === 'retry' ? null : a));
        }
    };
    // Skip takes effect on screen at once; the files are really dropped once the undo window has passed.
    const skip = () => {
        if (skipped || acting) return;
        setSkipped({ count: failedFiles.length || pendingUploadSummary?.fileCount || 0 });
        skipTimerRef.current = window.setTimeout(() => {
            skipTimerRef.current = null;
            void discardPersistedUploadSession().finally(() => { setSkipped(null); setRetryOf(null); });
        }, SKIP_UNDO_MS);
    };
    const undoSkip = () => {
        if (skipTimerRef.current !== null) window.clearTimeout(skipTimerRef.current);
        skipTimerRef.current = null;
        setSkipped(null);
    };
    const busy = uploading || acting !== null;
    const retryLabel = acting === 'retry' ? (showFailed ? 'Trying again…' : 'Resuming…') : showFailed ? 'Try again' : 'Resume';
    const pauseLabel = acting === 'pause' ? 'Pausing…' : 'Pause';
    const cancelLabel = acting === 'cancel' ? 'Cancelling…' : 'Cancel upload';

    if (skipped) {
        return (
            <ResultBar
                narrow={narrow}
                title={`${plural(skipped.count, 'item')} skipped`}
                sub="You can choose them again later"
                action={{ label: 'Undo', onClick: undoSkip }}
            />
        );
    }
    if (outcome && !cancelling && !uploadActive) {
        return (
            <ResultBar
                narrow={narrow}
                title="Cancelled"
                sub={`${outcome.added} added, ${outcome.notAdded} not`}
                action={{ label: 'Choose again', onClick: () => { setOutcome(null); requestUpload(); } }}
                onDismiss={() => setOutcome(null)}
            />
        );
    }

    const icon = (
        <span className={`up-ico${!uploadActive && showFailed ? ' bad' : ''}`}>
            {!uploadActive && showFailed ? <CircleAlert aria-hidden="true" /> : <CloudUpload aria-hidden="true" />}
        </span>
    );
    const stats = (
        <div className="up-stats">
            <div className="up-stat"><b>{added}</b><span>Added</span></div>
            <div className="up-stat"><b>{waiting}</b><span>Waiting</span></div>
            <div className={`up-stat${failed > 0 ? ' bad' : ''}`}><b>{failed}</b><span>To retry</span></div>
        </div>
    );
    const keepOpen = (
        <p className="up-note"><Info aria-hidden="true" /><span>Keep this page open until it finishes. If you close it, uploading stops and can continue from where it left off.</span></p>
    );
    const failureLede = (
        <>
            <p className="up-lede">These just need another try.</p>
            <p className="up-fine">If Try again can’t find them, choose Upload and pick the same photos again. Files already added are skipped.</p>
        </>
    );
    const retryingLede = retrying ? <p className="up-lede">{added} of {total} retried. {added} worked, {failed} failed again.</p> : null;
    const cancellingBody = (
        <>
            <Progress />
            <p className="up-lede">Stopping the last few files and clearing {cancelSnapshotRef.current.notAdded} from the list.</p>
            <div className="up-stats up-stats-2">
                <div className="up-stat"><b>{cancelSnapshotRef.current.added}</b><span>Stay added</span></div>
                <div className="up-stat"><b>{cancelSnapshotRef.current.notAdded}</b><span>To clear</span></div>
            </div>
            <p className="up-note"><Info aria-hidden="true" /><span>Photos already added are never removed. You can close this page.</span></p>
        </>
    );

    // One bar on phones; tapping it opens the detail in a sheet.
    if (narrow) {
        return (
            <>
                <div className="up-dock up-bar">
                    <button type="button" className="up-head" onClick={() => setSheetOpen(true)} aria-label={`${title}. ${sub}. Show details`}>
                        {icon}
                        <span className="up-t"><b>{title}</b><span>{sub}</span></span>
                        {uploadActive && !cancelling && !resuming ? <span className="up-pct">{pct}%</span> : null}
                        <ChevronRight className="up-chev" aria-hidden="true" />
                    </button>
                    {uploadActive || cancelling ? <Progress pct={cancelling || resuming ? undefined : pct} /> : null}
                </div>
                <BottomSheet open={sheetOpen} onClose={() => setSheetOpen(false)} title={heading}>
                    <div className="up-sheet">
                        {cancelling ? cancellingBody : uploadActive ? (
                            <>
                                <Progress pct={resuming ? undefined : pct} />
                                {retryingLede}
                                {checking ? <p className="up-lede">{checking.checked} of {checking.total} checked. Photos already in your library won’t be uploaded again.</p> : stats}
                                {!checking && duplicates > 0 ? <p className="up-fine">{duplicatesLabel}, so they were skipped.</p> : null}
                                {keepOpen}
                                <div className="up-btns">
                                    <Button onClick={pause} disabled={acting !== null}>{pauseLabel}</Button>
                                    <Button onClick={() => void cancel()} disabled={acting !== null}>{cancelLabel}</Button>
                                </div>
                            </>
                        ) : (
                            <>
                                {showFailed ? failureLede : <p className="up-lede">{plural(pendingUploadSummary?.fileCount ?? 0, 'file')} still waiting.</p>}
                                {showFailed ? <FailedRows files={failedFiles} /> : null}
                                <Button variant="primary" className="up-wide" onClick={() => void retry()} disabled={busy} aria-busy={acting === 'retry'}>{retryLabel}</Button>
                                <Button className="up-wide" onClick={skip} disabled={busy}>
                                    {showFailed ? `Skip ${failedFiles.length === 1 ? 'this one' : `these ${failedFiles.length}`}` : 'Discard'}
                                </Button>
                            </>
                        )}
                    </div>
                </BottomSheet>
            </>
        );
    }

    // A tray at the bottom right on desktop, collapsible to one line.
    return (
        <div className="up-dock up-tray" role="region" aria-label="Upload progress">
            <div className="up-uh">
                {icon}
                <b>{heading}</b>
                <IconButton label={collapsed ? 'Expand' : 'Collapse'} variant="link" size="sm" onClick={() => setCollapsed((c) => !c)} aria-expanded={!collapsed}>
                    <ChevronRight style={{ transform: `rotate(${collapsed ? -90 : 90}deg)` }} aria-hidden="true" />
                </IconButton>
                {uploadActive && !cancelling ? (
                    <IconButton label="Hide" variant="link" size="sm" onClick={() => setHidden(true)}><CloseIcon aria-hidden="true" /></IconButton>
                ) : null}
            </div>
            {cancelling ? (
                !collapsed ? <div className="up-ub">{cancellingBody}</div> : null
            ) : (
                <>
                    {uploadActive ? (
                        <div className="up-ub">
                            <Progress pct={resuming ? undefined : pct} />
                            {retryingLede}
                            <div className="up-fr"><span>{checking ? `${checking.checked} of ${checking.total} checked` : resuming ? `Picking up where you left off · ${plural(waiting, 'file')} left` : `${added} added · ${waiting} waiting`}</span><span>{checking ? 'Skipping ones you have' : timeLeft ?? duplicatesLabel ?? (progress?.mbPerSecond ? `${progress.mbPerSecond.toFixed(1)} MB/s` : '')}</span></div>
                        </div>
                    ) : null}
                    {!collapsed ? (
                        <>
                            {!uploadActive ? <div className="up-ub">{showFailed ? failureLede : <p className="up-lede">{plural(pendingUploadSummary?.fileCount ?? 0, 'file')} still waiting.</p>}</div> : null}
                            {showFailed ? <FailedRows files={failedFiles} onRetry={() => void retry()} retryDisabled={busy} /> : null}
                            <div className="up-uf">
                                {uploadActive ? (
                                    <>
                                        <span>Keep this tab open until it finishes.</span>
                                        <span className="up-fbtns">
                                            <button type="button" className="up-link" onClick={pause} disabled={acting !== null}>{pauseLabel}</button>
                                            <button type="button" className="up-link mut" onClick={() => void cancel()} disabled={acting !== null}>{acting === 'cancel' ? 'Cancelling…' : 'Cancel'}</button>
                                        </span>
                                    </>
                                ) : (
                                    <>
                                        <button type="button" className="up-link mut" onClick={skip} disabled={busy}>{showFailed ? 'Skip these' : 'Discard'}</button>
                                        <Button variant="primary" size="sm" onClick={() => void retry()} disabled={busy} aria-busy={acting === 'retry'}>{retryLabel}</Button>
                                    </>
                                )}
                            </div>
                        </>
                    ) : null}
                </>
            )}
        </div>
    );
};

export default UploadDock;
