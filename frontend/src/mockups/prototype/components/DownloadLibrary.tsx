import React, { useEffect, useState } from 'react';
import * as download from '../../../services/libraryDownload';
import { plural } from '../../../utils/format';

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

export const formatBytes = (bytes: number): string => {
    let value = Math.max(0, bytes);
    let unit = 0;
    while (value >= 1000 && unit < UNITS.length - 1) {
        value /= 1000;
        unit += 1;
    }
    return `${unit === 0 || value >= 100 ? Math.round(value) : value.toFixed(1)} ${UNITS[unit]}`;
};

export const formatDuration = (seconds: number): string => {
    if (seconds < 90) return 'about a minute';
    const minutes = Math.round(seconds / 60);
    if (minutes < 90) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    return `${hours} h ${minutes % 60} min`;
};

/** Library settings: copy every original into a folder on this computer. */
const DownloadLibrary: React.FC = () => {
    const [state, setState] = useState(download.getState());
    const [remembered, setRemembered] = useState('');
    useEffect(() => download.subscribe(setState), []);
    useEffect(() => {
        void download.rememberedFolderName().then(setRemembered).catch(() => undefined);
    }, [state.status]);

    if (!download.isFolderDownloadSupported()) {
        return (
            <p className="pt-page-sub" style={{ padding: '8px 4px 0' }}>
                Downloading the whole library needs Chrome or Edge on a computer.
            </p>
        );
    }

    const { status, progress, folderName } = state;
    const busy = status === 'starting' || status === 'running';
    const sized = !!progress && progress.bytesTotal > 0;
    const fraction = !progress ? 0 : sized
        ? Math.min(1, progress.bytesDone / progress.bytesTotal)
        : progress.filesTotal ? progress.filesDone / progress.filesTotal : 0;
    const folder = folderName || remembered;

    return (
        <div className="pt-download" style={{ marginTop: 12, padding: '0 4px' }}>
            <div className="pt-danger-row" style={{ marginTop: 0 }}>
                {busy ? (
                    <button type="button" className="btn" onClick={() => download.stop()}>Pause download</button>
                ) : (
                    <button type="button" className="btn" onClick={() => void download.start()}>
                        {folder && status !== 'done' ? `Resume download to “${folder}”` : 'Download library'}
                    </button>
                )}
                {!busy && folder && (
                    <button type="button" className="mock-link muted" onClick={() => void download.start(true)}>
                        Choose another folder
                    </button>
                )}
            </div>
            {status === 'idle' && !folder && (
                <p className="pt-page-sub">
                    Copies every original photo and video into a folder you choose, sorted by year and month.
                    You can pause and carry on later.
                </p>
            )}
            {status === 'starting' && <p className="pt-page-sub">Listing the library…</p>}
            {progress && status !== 'starting' && (
                <>
                    <div
                        role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(fraction * 100)}
                        aria-label="Library download"
                        style={{ height: 6, borderRadius: 3, background: 'var(--border, rgba(127,127,127,0.25))', margin: '10px 0 6px', overflow: 'hidden' }}
                    >
                        <div style={{ width: `${fraction * 100}%`, height: '100%', background: 'var(--accent, currentColor)' }} />
                    </div>
                    <p className="pt-page-sub">
                        {progress.filesDone.toLocaleString()} of {plural(progress.filesTotal, 'file')}
                        {sized && ` · ${formatBytes(progress.bytesDone)} of ${formatBytes(progress.bytesTotal)}`}
                        {status === 'running' && progress.rate > 0 && ` · ${formatBytes(progress.rate)}/s`}
                        {status === 'running' && progress.etaSeconds !== null && ` · ${formatDuration(progress.etaSeconds)} left`}
                    </p>
                </>
            )}
            {status === 'stopped' && <p className="pt-page-sub">Paused. Everything saved so far stays in the folder.</p>}
            {status === 'done' && (
                <p className="pt-page-sub">
                    Done. Every file is in “{folderName}”.
                    {progress && progress.unverified > 0 && ` ${plural(progress.unverified, 'file')} could not be checked against the upload record.`}
                </p>
            )}
            {status === 'error' && <p className="pt-page-sub" role="alert">{state.message}</p>}
        </div>
    );
};

export default DownloadLibrary;
