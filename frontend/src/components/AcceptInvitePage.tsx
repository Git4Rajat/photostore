import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import * as library from '../services/libraryClient';
import { Check as CheckIcon, Plus as PlusIcon, TriangleAlert as AlertIcon } from 'lucide-react';
import AuthShell from './shared/AuthShell';
import AuthField from './shared/AuthField';
import { MIN_PASSWORD_LENGTH } from './shared/passwordRules';

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
            if (password.length < MIN_PASSWORD_LENGTH) {
                setErrorMessage(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
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
            ? 'start using Keepsake'
            : 'join this library';

    const needsPassword = Boolean(info?.needsPassword);
    const longEnough = password.length >= MIN_PASSWORD_LENGTH;
    const matches = confirm.length > 0 && password === confirm;
    const problem = (text: string) => (
        <p className="auth-msg box" role="alert"><AlertIcon aria-hidden="true" /><span>{text}</span></p>
    );

    return (
        <AuthShell title="Accept your invitation">
            {!token ? (
                problem('This link is missing its invitation token.')
            ) : loading ? (
                <p className="auth-note">Checking your invitation…</p>
            ) : !info?.valid ? (
                problem('This invitation is invalid or has expired. Ask for a new one.')
            ) : done ? (
                <div className="auth-center">
                    <p className="auth-note ok">You're all set. Welcome to Keepsake!</p>
                    {/* Hard navigation so the app re-bootstraps auth state from the
                        session token the acceptance just stored. */}
                    <button type="button" className="auth-btn top" onClick={() => { window.location.href = '/'; }}>
                        Go to your photos
                    </button>
                </div>
            ) : (
                <form className="auth-form" onSubmit={handleSubmit} noValidate>
                    <p className="auth-note">
                        {info.email ? <strong>{info.email}</strong> : 'You'} — you've been invited to {targetLabel}.
                    </p>
                    {errorMessage && problem(errorMessage)}
                    {needsPassword ? (
                        <>
                            <AuthField id="invite-password" label="Choose a password" type="password" autoComplete="new-password" value={password} onChange={setPassword} />
                            <AuthField id="invite-confirm" label="Confirm password" type="password" autoComplete="new-password" value={confirm} onChange={setConfirm} />
                            <ul className="auth-rules" aria-label="Password rules">
                                <li className={longEnough ? 'ok' : undefined}>{longEnough ? <CheckIcon aria-hidden="true" /> : <PlusIcon aria-hidden="true" />}At least {MIN_PASSWORD_LENGTH} characters</li>
                                <li className={matches ? 'ok' : undefined}>{matches ? <CheckIcon aria-hidden="true" /> : <PlusIcon aria-hidden="true" />}Both passwords match</li>
                            </ul>
                        </>
                    ) : (
                        <p className="auth-note">
                            To accept, make sure you're signed in as {info.email || 'the invited account'}.
                        </p>
                    )}
                    <button
                        type="submit"
                        className="auth-btn"
                        disabled={pending || (needsPassword && (!longEnough || !matches))}
                    >
                        {pending ? 'Accepting…' : 'Accept invitation'}
                    </button>
                    {/* Hard navigation: this page can render under a router that
                        only knows the /accept-invite route (see PrototypeApp). */}
                    <button
                        type="button"
                        className="auth-link quiet auth-back"
                        onClick={() => { window.location.href = '/login'; }}
                    >
                        Back to sign in
                    </button>
                </form>
            )}
        </AuthShell>
    );
};

export default AcceptInvitePage;
