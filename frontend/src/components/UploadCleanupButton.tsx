import React, { useEffect, useRef, useState } from 'react';
import { TrashIcon } from '@heroicons/react/24/outline';
import { post, postUpload } from '../services/apiClient';
import { sessionScope } from '../services/sessionScope';

interface CleanupResult {
    deleted: number;
    trackingCleared: number;
    skipped: number;
    errors: Array<{ filename: string; error: string }>;
    more: boolean;
    nextCursor: string;
}

const REQUEST = { singleAttempt: true, timeout: 120000 };

export const UploadCleanupButton: React.FC<{ uploadActive: boolean; disabled?: boolean }> = ({ uploadActive, disabled }) => {
    const [running, setRunning] = useState(false);
    const [message, setMessage] = useState('');
    const [failed, setFailed] = useState(false);
    const mounted = useRef(true);
    const busy = useRef(false);
    useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

    const clean = async () => {
        if (busy.current || uploadActive || disabled) return;
        busy.current = true;
        setRunning(true);
        setFailed(false);
        setMessage('Removing abandoned upload data…');
        const scope = sessionScope();
        let after = '';
        let deleted = 0;
        let cleared = 0;
        let skipped = 0;
        let failures = 0;
        try {
            const recovery = await post<{ pending?: boolean }>('/api/upload-sessions/reconcile', {}, REQUEST);
            if (!mounted.current || sessionScope() !== scope) return;
            if (recovery.pending) throw new Error('Upload recovery is still finishing. Try cleanup again shortly.');
            for (let page = 0; page < 100; page += 1) {
                if (!mounted.current || sessionScope() !== scope) return;
                const result = await postUpload<CleanupResult>('/upload/cleanup', { after }, REQUEST);
                if (!mounted.current || sessionScope() !== scope) return;
                deleted += result.deleted;
                cleared += result.trackingCleared;
                skipped += result.skipped;
                failures += result.errors.length;
                const summary = `${deleted} unfinished upload records removed; ${cleared} upload markers cleared.`;
                setMessage(summary);
                if (!result.more || page === 99) {
                    setFailed(failures > 0);
                    setMessage(summary
                        + (skipped ? ` ${skipped} recent or busy records skipped.` : '')
                        + (failures ? ` ${failures} records could not be cleaned. Run cleanup again to retry.` : '')
                        + (result.more ? ' More records remain. Run cleanup again to continue.' : ''));
                    return;
                }
                if (!result.nextCursor || result.nextCursor === after) throw new Error('Cleanup did not advance. Run cleanup again.');
                after = result.nextCursor;
            }
        } catch (error) {
            if (mounted.current && sessionScope() === scope) {
                setFailed(true);
                const detail = error instanceof Error ? error.message : 'Upload cleanup could not finish.';
                setMessage(`${deleted} unfinished upload records removed; ${cleared} upload markers cleared. ${detail}`);
            }
        } finally {
            busy.current = false;
            if (mounted.current) setRunning(false);
        }
    };

    return <div className="tools-panel">
        <div className="tools-panel-header"><h2 className="tools-panel-title">Upload cleanup</h2></div>
        <p className="tools-panel-meta">Remove abandoned upload data and stale upload markers. Completed photos are kept.</p>
        <button type="button" className="btn btn-soft tools-admin-button" disabled={running || uploadActive || disabled}
            onClick={() => void clean()}>
            <TrashIcon className="toolbar-icon" aria-hidden="true" />
            {running ? 'Cleaning uploads…' : 'Upload cleanup'}
        </button>
        {uploadActive && <p className="status">Finish or stop the current upload before running cleanup.</p>}
        {message && <p role="status" aria-live="polite" className={`status${failed ? ' error' : ''}`}>{message}</p>}
    </div>;
};
