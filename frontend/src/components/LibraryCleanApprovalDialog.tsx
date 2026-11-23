import React, { useEffect, useState } from 'react';
import * as library from '../services/libraryClient';
import { Button } from './ui/Button';

export const CONFIRM_LIBRARY_CLEAN_PATH = '/confirm-library-clean';

// The emailed link lands in the signed-in app at /confirm-library-clean?token=…;
// the token is read once, and the URL is kept until the dialog is closed so a
// reload still shows the same approval.
const tokenFromLocation = (): string | null => {
    if (typeof window === 'undefined' || window.location.pathname !== CONFIRM_LIBRARY_CLEAN_PATH) return null;
    return new URLSearchParams(window.location.search).get('token') || '';
};

// Opening a link only reads status. Approval always requires an explicit click
// by the authenticated recipient; consumed approvals remain readable on reload.
const LibraryCleanApprovalDialog: React.FC = () => {
    const [token, setToken] = useState<string | null>(tokenFromLocation);
    if (token === null) return null;
    return <ApprovalDialogBody token={token} onClose={() => {
        window.history.replaceState(null, '', '/');
        setToken(null);
    }} />;
};

const ApprovalDialogBody: React.FC<{ token: string; onClose: () => void }> = ({ token, onClose }) => {
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

    const title = status?.status === 'pending' ? 'Confirm library cleanup' : 'Library cleanup status';
    return (
        <div className="dialog-backdrop">
            <div className="dialog-card" role="dialog" aria-modal="true" aria-label={title}>
                <p className="dialog-title">{title}</p>
                {!token ? (
                    <p className="status error">This link is missing its confirmation token.</p>
                ) : (
                    <>
                        {errorMessage && <p className="status error">{errorMessage}</p>}
                        {!status ? (
                            !errorMessage && <p className="status">Checking cleanup status…</p>
                        ) : status.status === 'pending' ? (
                            <>
                                {approvalError && <p className="status error">{approvalError}</p>}
                                <p className="status error">
                                    This will permanently delete all photos and videos in this library. This cannot be undone.
                                </p>
                            </>
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
                    </>
                )}
                <div className="dialog-actions">
                    {token && (errorMessage || status?.status === 'unknown') && (
                        <Button variant="soft" onClick={() => setRefresh((value) => value + 1)}>Refresh status</Button>
                    )}
                    <Button variant="soft" onClick={onClose}>
                        {status?.status === 'pending' ? 'Not now' : 'Close'}
                    </Button>
                    {status?.status === 'pending' && (
                        <Button variant="danger" disabled={confirming} onClick={handleConfirm}>
                            {confirming ? 'Confirming…' : 'Confirm cleanup'}
                        </Button>
                    )}
                </div>
            </div>
        </div>
    );
};

export default LibraryCleanApprovalDialog;
