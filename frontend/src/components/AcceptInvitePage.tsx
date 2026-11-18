import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import * as library from '../services/libraryClient';
import { Check as CheckIcon, Link2Off as LinkOffIcon, Plus as PlusIcon, TriangleAlert as AlertIcon } from 'lucide-react';
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
    const [libraryName, setLibraryName] = useState('');
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
        const ownLibrary = needsPassword && info?.targetType !== 'join';
        if (needsPassword) {
            if (password.length < MIN_PASSWORD_LENGTH) {
                setErrorMessage(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
                return;
            }
        }
        setPending(true);
        try {
            await library.acceptInvite(token, needsPassword ? password : undefined, ownLibrary ? libraryName : undefined);
            setDone(true);
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Could not accept this invitation.');
        } finally {
            setPending(false);
        }
    };

    const needsPassword = Boolean(info?.needsPassword);
    // The mock-up's "First time" screen: an invited person who starts their own library.
    const ownLibrary = needsPassword && info?.targetType !== 'join';
    const longEnough = password.length >= MIN_PASSWORD_LENGTH;
    const problem = (text: string) => (
        <p className="auth-msg box" role="alert"><AlertIcon aria-hidden="true" /><span>{text}</span></p>
    );
    const goToSignIn = () => { window.location.href = '/login'; };

    const endedLink = !token || (!loading && !info?.valid);
    const title = endedLink
        ? 'Invitation'
        : ownLibrary
            ? 'Set up your library'
            : info?.libraryName ? `Join ${info.libraryName}` : 'Accept your invitation';
    const subtitle = endedLink || done
        ? undefined
        : ownLibrary
            ? "You'll be the owner. You can invite others later."
            : needsPassword ? 'Choose a password to accept your invitation.' : undefined;

    const rules = (
        <ul className="auth-rules" aria-label="Password rules">
            <li className={longEnough ? 'ok' : undefined}>{longEnough ? <CheckIcon aria-hidden="true" /> : <PlusIcon aria-hidden="true" />}At least {MIN_PASSWORD_LENGTH} characters</li>
        </ul>
    );

    return (
        <AuthShell title={title} subtitle={subtitle}>
            {endedLink ? (
                <div className="auth-center">
                    <span className="auth-bigicon"><LinkOffIcon aria-hidden="true" /></span>
                    <h2>This link no longer works</h2>
                    <p className="auth-note">It may have expired, or it has already been used.</p>
                    <p className="auth-note">Ask for a new invitation.</p>
                    <button type="button" className="auth-btn top" onClick={goToSignIn}>Back to sign in</button>
                </div>
            ) : loading ? (
                <p className="auth-note">Checking your invitation…</p>
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
                    {!ownLibrary && !needsPassword && (
                        <p className="auth-note">
                            {info?.email ? <strong>{info.email}</strong> : 'You'}: you've been invited to join this library.
                            To accept, make sure you're signed in as {info?.email || 'the invited account'}.
                        </p>
                    )}
                    {errorMessage && problem(errorMessage)}
                    {ownLibrary && (
                        <AuthField id="invite-library-name" label="Library name" value={libraryName} onChange={setLibraryName} autoComplete="off" />
                    )}
                    {needsPassword && (
                        <>
                            <AuthField id="invite-email" label="Email address" type="email" value={info?.email || ''} onChange={() => {}} readOnly autoComplete="username" />
                            <AuthField id="invite-password" label="Password" type="password" autoComplete="new-password" value={password} onChange={setPassword} />
                            {rules}
                        </>
                    )}
                    <button type="submit" className="auth-btn top" disabled={pending || (needsPassword && !longEnough)}>
                        {pending ? (ownLibrary ? 'Creating…' : 'Accepting…') : ownLibrary ? 'Create library' : 'Accept invite'}
                    </button>
                    {/* Hard navigation: this page can render under a router that
                        only knows the /accept-invite route (see PrototypeApp). */}
                    {!ownLibrary && (
                        <button type="button" className="auth-link quiet auth-back" onClick={goToSignIn}>
                            Already use Keepsake? Sign in to accept
                        </button>
                    )}
                </form>
            )}
        </AuthShell>
    );
};

export default AcceptInvitePage;
