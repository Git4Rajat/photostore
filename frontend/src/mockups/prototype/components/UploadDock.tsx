import React, { useEffect, useRef, useState } from 'react';
import { ChevronRight, CircleAlert, CloudUpload, Image as PhotoIcon, Info, X as CloseIcon } from 'lucide-react';
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

const Progress: React.FC<{ pct: number; bad?: boolean }> = ({ pct, bad }) => (
    <div className={`up-prog${bad ? ' bad' : ''}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label="Upload progress">
        <i style={{ width: `${pct}%` }} />
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
        stopActiveUpload, retryPersistedUploadSession, discardPersistedUploadSession,
    } = useAppServices();
    const narrow = useNarrow();
    const [sheetOpen, setSheetOpen] = useState(false);
    const [collapsed, setCollapsed] = useState(false);
    const [hidden, setHidden] = useState(false);
    const [cancelling, setCancelling] = useState(false);
    const startRef = useRef<{ at: number; base: number } | null>(null);

    const added = progress?.uploadedCount ?? 0;
    const total = progress?.totalCount ?? 0;
    const failed = progress?.failedCount ?? 0;
    const pending = !uploadActive && (pendingUploadSummary?.fileCount ?? 0) > 0;
    const failedFiles = pendingUploadFailedFiles;
    const showFailed = pending && failedFiles.length > 0;
    const visible = uploadActive ? !hidden : pending;

    useEffect(() => {
        if (!uploadActive) { startRef.current = null; setHidden(false); return; }
        if (!startRef.current) startRef.current = { at: Date.now(), base: added };
    }, [uploadActive, added]);

    // Cancel = pause, then drop what is left once the pause has landed.
    useEffect(() => {
        if (!cancelling || uploadActive) return;
        setCancelling(false);
        if (pendingUploadSummary) void discardPersistedUploadSession();
    }, [cancelling, uploadActive, pendingUploadSummary, discardPersistedUploadSession]);

    useEffect(() => { if (!visible) setSheetOpen(false); }, [visible]);

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
    const label = uploading ? 'Uploading' : 'Finishing upload';
    const title = checking && uploadActive
        ? `Checking ${plural(checking.total, 'photo')}`
        : !uploadActive
        ? (showFailed ? `${plural(failedFiles.length, 'item')} didn’t upload` : 'Upload paused')
        : total ? `${label} ${added} of ${total}` : `${label}…`;
    const sub = !uploadActive
        ? (showFailed ? 'Tap to see why' : `${plural(pendingUploadSummary?.fileCount ?? 0, 'file')} waiting`)
        : checking ? 'Looking for ones you already have'
        : [timeLeft ?? (progress?.mbPerSecond ? `${progress.mbPerSecond.toFixed(1)} MB/s` : 'Starting…'), duplicatesLabel].filter(Boolean).join(' · ');

    const heading = uploadActive ? (!checking && uploading && total ? `Uploading ${plural(total, 'item')}` : title) : title;
    const pause = () => stopActiveUpload();
    const cancel = async () => {
        const ok = await confirmDialog({
            title: 'Cancel this upload?',
            message: 'Files already added stay in Photos. The rest won’t be uploaded.',
            confirmLabel: 'Cancel upload',
            cancelLabel: 'Keep uploading',
            danger: true,
        });
        if (!ok) return;
        setCancelling(true);
        stopActiveUpload();
    };
    const retry = () => { void retryPersistedUploadSession(); };
    const skip = () => { void discardPersistedUploadSession(); };

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

    // One bar on phones; tapping it opens the detail in a sheet.
    if (narrow) {
                return (
            <>
                <div className="up-dock up-bar">
                    <button type="button" className="up-head" onClick={() => setSheetOpen(true)} aria-label={`${title}. ${sub}. Show details`}>
                        {icon}
                        <span className="up-t"><b>{title}</b><span>{sub}</span></span>
                        {uploadActive ? <span className="up-pct">{pct}%</span> : null}
                        <ChevronRight className="up-chev" aria-hidden="true" />
                    </button>
                    {uploadActive ? <Progress pct={pct} /> : null}
                </div>
                <BottomSheet open={sheetOpen} onClose={() => setSheetOpen(false)} title={heading}>
                    <div className="up-sheet">
                        {uploadActive ? (
                            <>
                                <Progress pct={pct} />
                                {checking ? <p className="up-lede">{checking.checked} of {checking.total} checked. Photos already in your library won’t be uploaded again.</p> : stats}
                                {!checking && duplicates > 0 ? <p className="up-fine">{duplicatesLabel}, so they were skipped.</p> : null}
                                {keepOpen}
                                <div className="up-btns">
                                    <Button onClick={pause}>Pause</Button>
                                    <Button onClick={() => void cancel()}>Cancel upload</Button>
                                </div>
                            </>
                        ) : (
                            <>
                                {showFailed ? failureLede : <p className="up-lede">{plural(pendingUploadSummary?.fileCount ?? 0, 'file')} still waiting.</p>}
                                {showFailed ? <FailedRows files={failedFiles} /> : null}
                                <Button variant="primary" className="up-wide" onClick={retry} disabled={uploading}>{showFailed ? 'Try again' : 'Resume'}</Button>
                                <Button className="up-wide" onClick={skip} disabled={uploading}>
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
                {uploadActive ? (
                    <IconButton label="Hide" variant="link" size="sm" onClick={() => setHidden(true)}><CloseIcon aria-hidden="true" /></IconButton>
                ) : null}
            </div>
            {uploadActive ? (
                <div className="up-ub">
                    <Progress pct={pct} />
                    <div className="up-fr"><span>{checking ? `${checking.checked} of ${checking.total} checked` : `${added} added · ${waiting} waiting`}</span><span>{checking ? 'Skipping ones you have' : timeLeft ?? duplicatesLabel ?? (progress?.mbPerSecond ? `${progress.mbPerSecond.toFixed(1)} MB/s` : '')}</span></div>
                </div>
            ) : null}
            {!collapsed ? (
                <>
                    {!uploadActive ? <div className="up-ub">{showFailed ? failureLede : <p className="up-lede">{plural(pendingUploadSummary?.fileCount ?? 0, 'file')} still waiting.</p>}</div> : null}
                    {showFailed ? <FailedRows files={failedFiles} onRetry={retry} retryDisabled={uploading} /> : null}
                    <div className="up-uf">
                        {uploadActive ? (
                            <>
                                <span>Keep this tab open until it finishes.</span>
                                <span className="up-fbtns">
                                    <button type="button" className="up-link" onClick={pause}>Pause</button>
                                    <button type="button" className="up-link mut" onClick={() => void cancel()}>Cancel</button>
                                </span>
                            </>
                        ) : (
                            <>
                                <button type="button" className="up-link mut" onClick={skip} disabled={uploading}>{showFailed ? 'Skip these' : 'Discard'}</button>
                                <Button variant="primary" size="sm" onClick={retry} disabled={uploading}>{showFailed ? 'Try again' : 'Resume'}</Button>
                            </>
                        )}
                    </div>
                </>
            ) : null}
        </div>
    );
};

export default UploadDock;
