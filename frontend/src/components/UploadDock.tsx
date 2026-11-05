import React from 'react';
import { useAppServices } from './AppServicesProvider';

export const UploadDock: React.FC = () => {
    const { uploadActive, uploading, uploadProgress: progress, pendingUploadSummary, stopActiveUpload } = useAppServices();
    const showFailed = !uploadActive && (pendingUploadSummary?.failedCount ?? 0) > 0;
    if (!uploadActive && !showFailed) return null;
    const pct = progress?.totalCount ? Math.round((progress.uploadedCount / progress.totalCount) * 100) : 0;
    const label = uploading ? 'Uploading' : 'Finishing upload';
    return (
        <div className={`upload-dock${!uploadActive ? ' is-done' : ''}`}>
            {uploadActive ? (
                <>
                    <span className="count">{progress ? `${label} ${progress.uploadedCount} / ${progress.totalCount}` : `${label}…`}</span>
                    <span className="track"><span className="fill" style={{ width: `${pct}%` }} /></span>
                    {progress?.mbPerSecond ? <span className="rate">{progress.mbPerSecond.toFixed(1)} MB/s</span> : null}
                    <button type="button" className="btn" onClick={stopActiveUpload}>Stop</button>
                </>
            ) : (
                <>
                    <span className="done-label">Upload finished</span>
                    <span className="track"><span className="fill" style={{ width: '100%' }} /></span>
                    <span className="failed">{pendingUploadSummary?.failedCount} failed</span>
                </>
            )}
        </div>
    );
};
