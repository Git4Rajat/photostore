import React, { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import * as library from '../services/libraryClient';
import { LogoLockup } from './shared/Logo';

// Public page reached from the "confirm cleanup" link in a library-clean
// confirmation email:
//   /confirm-library-clean?token=<token>
// The caller must be signed in as the approver the link was addressed to;
// the backend enforces that and returns a 401/403 otherwise.
const ConfirmLibraryCleanPage: React.FC = () => {
    const [searchParams] = useSearchParams();
    const token = searchParams.get('token') || '';
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [outcome, setOutcome] = useState<'idle' | 'awaiting_more_approvals' | 'queued' | 'done' | 'failed'>('idle');
    const [jobId, setJobId] = useState('');
    const [statusResult, setStatusResult] = useState<library.CleanStatusResult | null>(null);
    const pollTimer = useRef<number | null>(null);

    const handleConfirm = async () => {
        setErrorMessage('');
        setPending(true);
        try {
            const result = await library.confirmLibraryClean(token);
            if (result.status === 'awaiting_more_approvals') {
                setOutcome('awaiting_more_approvals');
            } else if (result.jobId) {
                setJobId(result.jobId);
                setOutcome(result.queued ? 'queued' : 'done');
            } else {
                setOutcome('failed');
                setErrorMessage('The cleanup could not be started.');
            }
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Could not confirm the cleanup.');
        } finally {
            setPending(false);
        }
    };

    useEffect(() => {
        if (!jobId || outcome === 'done' || outcome === 'failed') return undefined;
        const poll = async () => {
            try {
                const result = await library.getLibraryCleanStatus(jobId);
                setStatusResult(result);
                if (result.status === 'done' || result.status === 'failed') {
                    setOutcome(result.status as 'done' | 'failed');
                    return;
                }
            } catch {
                // Keep polling; a transient status-check failure isn't fatal.
            }
            pollTimer.current = window.setTimeout(poll, 3000);
        };
        poll();
        return () => {
            if (pollTimer.current) window.clearTimeout(pollTimer.current);
        };
    }, [jobId, outcome]);

    return (
        <section className="auth-page card-glass">
            <LogoLockup size={46} className="auth-logo" />
            <p className="additional-kicker">CONFIRM CLEANUP</p>
            <h2 className="auth-page-title">Confirm library cleanup</h2>
            {!token ? (
                <p className="status error">This link is missing its confirmation token.</p>
            ) : outcome === 'idle' ? (
                <div className="local-login">
                    <p className="status error">
                        This will permanently delete all photos and videos in this library. This cannot be undone.
                    </p>
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    <button type="button" className="btn btn-soft danger" disabled={pending} onClick={handleConfirm}>
                        {pending ? 'Confirming…' : 'Confirm cleanup'}
                    </button>
                    <Link className="btn btn-link auth-page-link" to="/login">Back to sign in</Link>
                </div>
            ) : outcome === 'awaiting_more_approvals' ? (
                <p className="status success">
                    Your confirmation was recorded. Cleanup will start once every required approver has confirmed.
                </p>
            ) : outcome === 'failed' ? (
                <p className="status error">{statusResult?.error || errorMessage || 'The cleanup failed.'}</p>
            ) : outcome === 'done' ? (
                <p className="status success">
                    Cleanup complete
                    {statusResult?.result?.photosDeleted != null ? ` — ${statusResult.result.photosDeleted} photo(s)/video(s) removed.` : '.'}
                </p>
            ) : (
                <p className="status">Cleanup is running in the background…</p>
            )}
        </section>
    );
};

export default ConfirmLibraryCleanPage;
