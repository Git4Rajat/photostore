import React, { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import * as library from '../services/libraryClient';

// Public page reached from the "accept your invitation" link in an invite email:
//   /accept-invite?token=<token>
// New accounts (password mode) set a password here; existing accounts confirm
// while signed in as the invited address.
const AcceptInvitePage: React.FC = () => {
    const [searchParams] = useSearchParams();
    const token = searchParams.get('token') || '';
    const [info, setInfo] = useState<library.InviteInfo | null>(null);
    const [loading, setLoading] = useState(true);
    const [password, setPassword] = useState('');
    const [confirm, setConfirm] = useState('');
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [done, setDone] = useState(false);

    useEffect(() => {
        let active = true;
        (async () => {
            if (!token) {
                setLoading(false);
                return;
            }
            try {
                const result = await library.getInviteInfo(token);
                if (active) {
                    setInfo(result);
                }
            } finally {
                if (active) {
                    setLoading(false);
                }
            }
        })();
        return () => {
            active = false;
        };
    }, [token]);

    const handleSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        setErrorMessage('');
        const needsPassword = Boolean(info?.needsPassword);
        if (needsPassword) {
            if (password.length < 8) {
                setErrorMessage('Password must be at least 8 characters.');
                return;
            }
            if (password !== confirm) {
                setErrorMessage('Passwords do not match.');
                return;
            }
        }
        setPending(true);
        try {
            await library.acceptInvite(token, needsPassword ? password : undefined);
            setDone(true);
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Could not accept this invitation.');
        } finally {
            setPending(false);
        }
    };

    const targetLabel = info?.libraryName
        ? `join ${info.libraryName}`
        : info?.targetType === 'fresh'
            ? 'start using Photostore'
            : 'join this library';

    return (
        <section className="auth-page card-glass">
            <p className="additional-kicker">INVITATION</p>
            <h2 className="auth-page-title">Accept your invitation</h2>
            {!token ? (
                <p className="status error">This link is missing its invitation token.</p>
            ) : loading ? (
                <p className="status">Checking your invitation…</p>
            ) : !info?.valid ? (
                <p className="status error">This invitation is invalid or has expired. Ask for a new one.</p>
            ) : done ? (
                <>
                    <p className="status success">You're all set. Welcome to Photostore!</p>
                    {/* Hard navigation so the app re-bootstraps auth state from the
                        session token the acceptance just stored. */}
                    <button type="button" className="btn btn-primary" onClick={() => { window.location.href = '/'; }}>
                        Go to your photos
                    </button>
                </>
            ) : (
                <form className="local-login" onSubmit={handleSubmit}>
                    <p className="status">
                        {info.email ? <strong>{info.email}</strong> : 'You'} — you've been invited to {targetLabel}.
                    </p>
                    {info.needsPassword ? (
                        <>
                            <label htmlFor="invite-password">Choose a password</label>
                            <input
                                id="invite-password"
                                className="field"
                                type="password"
                                autoComplete="new-password"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                            />
                            <label htmlFor="invite-confirm">Confirm password</label>
                            <input
                                id="invite-confirm"
                                className="field"
                                type="password"
                                autoComplete="new-password"
                                value={confirm}
                                onChange={(e) => setConfirm(e.target.value)}
                            />
                        </>
                    ) : (
                        <p className="status">
                            To accept, make sure you're signed in as {info.email || 'the invited account'}.
                        </p>
                    )}
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    <button
                        type="submit"
                        className="btn btn-primary"
                        disabled={pending || (Boolean(info.needsPassword) && (!password || !confirm))}
                    >
                        {pending ? 'Accepting…' : 'Accept invitation'}
                    </button>
                    <Link className="btn btn-link auth-page-link" to="/login">Back to sign in</Link>
                </form>
            )}
        </section>
    );
};

export default AcceptInvitePage;
