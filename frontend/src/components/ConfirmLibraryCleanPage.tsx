import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import * as library from '../services/libraryClient';
import AuthShell from './shared/AuthShell';

// Opening a link only reads status. Approval always requires an explicit click
// by the authenticated recipient; consumed approvals remain readable on reload.
const ConfirmLibraryCleanPage: React.FC = () => {
    const [searchParams] = useSearchParams();
    const token = searchParams.get('token') || '';
    const [confirming, setConfirming] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [approvalError, setApprovalError] = useState('');
    const [status, setStatus] = useState<library.CleanConfirmationStatus | null>(null);
    const [refresh, setRefresh] = useState(0);

    useEffect(() => { setApprovalError(''); }, [token]);

    useEffect(() => {
        setStatus(null);
        setErrorMessage('');
        if (!token) return;
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout> | undefined;
        let failures = 0;
        const poll = async () => {
            try {
                const result = await library.getLibraryCleanConfirmationStatus(token);
                if (cancelled) return;
                failures = 0;
                setStatus(result);
                setErrorMessage('');
                if (['queued', 'running', 'awaiting_more_approvals', 'awaiting_start'].includes(result.status)) {
                    timer = setTimeout(() => { void poll(); }, 3000);
                }
            } catch (error) {
                if (cancelled) return;
                setErrorMessage(error instanceof Error ? error.message : 'Could not load cleanup status.');
                // Keep the last known status during temporary failures. Never
                // turn a failed status lookup into another confirmation form.
                failures += 1;
                if (failures < 5) timer = setTimeout(() => { void poll(); }, 3000);
            }
        };
        void poll();
        return () => {
            cancelled = true;
            clearTimeout(timer);
        };
    }, [token, refresh]);

    const handleConfirm = async () => {
        if (confirming) return;
        setConfirming(true);
        setErrorMessage('');
        setApprovalError('');
        try {
            await library.confirmLibraryClean(token);
        } catch (error) {
            setApprovalError(error instanceof Error ? error.message : 'Could not confirm the cleanup.');
        } finally {
            // Also resolve an ambiguous POST outcome (e.g. its response was
            // lost after approval): the saved request remains authoritative.
            setStatus(null);
            setRefresh((value) => value + 1);
            setConfirming(false);
        }
    };

    return (
        <AuthShell kicker="LIBRARY CLEANUP" title={status?.status === 'pending' ? 'Confirm library cleanup' : 'Library cleanup status'}>
            {!token ? (
                <p className="status error">This link is missing its confirmation token.</p>
            ) : (
                <>
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    {!status ? (
                        !errorMessage && <p className="status">Checking cleanup status…</p>
                    ) : status.status === 'pending' ? (
                        <div className="local-login">
                            {approvalError && <p className="status error">{approvalError}</p>}
                            <p className="status error">
                                This will permanently delete all photos and videos in this library. This cannot be undone.
                            </p>
                            <button type="button" className="btn btn-soft danger" disabled={confirming} onClick={handleConfirm}>
                                {confirming ? 'Confirming…' : 'Confirm cleanup'}
                            </button>
                        </div>
                    ) : status.status === 'awaiting_more_approvals' ? (
                        <p className="status success">Your confirmation was recorded. Cleanup will start once every required approver has confirmed.</p>
                    ) : status.status === 'awaiting_start' ? (
                        <p className="status">Your confirmation was recorded. Waiting for cleanup to start…</p>
                    ) : status.status === 'failed' ? (
                        <p className="status error">{status.error || 'The cleanup failed.'}</p>
                    ) : status.status === 'done' ? (
                        <p className="status success">
                            Cleanup complete
                            {status.result?.photosDeleted != null ? ` — ${status.result.photosDeleted} photo(s)/video(s) removed.` : '.'}
                        </p>
                    ) : ['queued', 'running'].includes(status.status) ? (
                        <p className="status">Cleanup is {status.status === 'queued' ? 'queued' : 'running in the background'}…</p>
                    ) : (
                        <p className="status">The saved cleanup status is currently unavailable.</p>
                    )}
                    {(errorMessage || status?.status === 'unknown') && (
                        <button type="button" className="btn btn-soft" onClick={() => setRefresh((value) => value + 1)}>Refresh status</button>
                    )}
                </>
            )}
            <button type="button" className="btn btn-link auth-page-link" onClick={() => { window.location.href = '/login'; }}>
                Back to sign in
            </button>
        </AuthShell>
    );
};

export default ConfirmLibraryCleanPage;
