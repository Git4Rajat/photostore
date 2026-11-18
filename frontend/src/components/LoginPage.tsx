import React, { useState } from 'react';
import { ArrowLeft as ArrowLeftIcon, LogIn as ArrowRightOnRectangleIcon, Mail as MailIcon, TriangleAlert as AlertIcon } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { setUserId } from '../services/apiClient';
import { getRuntimeConfig } from '../config/appConfig';
import * as passwordAuth from '../services/passwordAuthClient';
import AuthShell from './shared/AuthShell';
import AuthField from './shared/AuthField';

interface LoginPageProps {
    authEnabled: boolean;
    authReady: boolean;
    displayName: string;
    onSignIn: () => Promise<void>;
    // Called after a successful password login so the app can refresh auth state.
    onAuthenticated?: () => Promise<void>;
}

const isPasswordMode = (): boolean => (getRuntimeConfig().authMode || '').toLowerCase() === 'password';

// Lightweight email format check (the backend is single-owner and doesn't
// validate the address, so guard it on the client).
const isValidEmail = (value: string): boolean => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(value.trim());

const FIELD_IDS = { email: 'owner-email', password: 'owner-password' };

const PasswordLogin: React.FC<{ initialEmail?: string; onAuthenticated?: () => Promise<void>; onResetSent: (email: string) => void }> = ({ initialEmail, onAuthenticated, onResetSent }) => {
    const [email, setEmail] = useState(() => initialEmail || passwordAuth.getEmailHint());
    const [password, setPassword] = useState('');
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    // Which fields the message is about: a failed sign-in does not say which half was wrong, so both are marked.
    const [badFields, setBadFields] = useState<Array<'email' | 'password'>>([]);

    const fail = (message: string, fields: Array<'email' | 'password'>) => {
        setErrorMessage(message);
        setBadFields(fields);
    };

    const handleLogin = async (event: React.FormEvent) => {
        event.preventDefault();
        setErrorMessage('');
        setBadFields([]);
        if (!isValidEmail(email)) {
            fail('Enter a valid email address.', ['email']);
            return;
        }
        setPending(true);
        try {
            await passwordAuth.login(email.trim(), password);
            setPassword('');
            if (onAuthenticated) {
                await onAuthenticated();
            }
            // Full reload rather than an SPA navigate: the store and the
            // per-library caches are populated at boot, so a signed-in reboot
            // is the clean way to load exactly this account's library. An SPA
            // navigate would leave any previous session's in-memory photos in
            // place (see clientDataReset.ts / the 2026-10-17 account-switch bug).
            window.location.assign('/');
        } catch (error) {
            // What was typed stays; only the message and the field marks change.
            const message = error instanceof Error ? error.message : passwordAuth.SIGN_IN_FAILED_MESSAGE;
            fail(message, message === passwordAuth.SIGN_IN_FAILED_MESSAGE ? ['email', 'password'] : []);
        } finally {
            setPending(false);
        }
    };

    const handleForgot = async () => {
        setErrorMessage('');
        setBadFields([]);
        if (!isValidEmail(email)) {
            fail('Enter your email address first, then choose Forgot password.', ['email']);
            return;
        }
        try {
            await passwordAuth.requestPasswordReset(email.trim());
        } catch {
            // Swallowed on purpose: the outcome must not change what the page says.
        } finally {
            // Always show the same confirmation regardless of outcome so we
            // never reveal whether an address is actually on file.
            onResetSent(email.trim());
        }
    };

    return (
        <form className="auth-form" onSubmit={handleLogin} noValidate>
            {errorMessage && (
                <p className="auth-msg box" role="alert" id="auth-error">
                    <AlertIcon aria-hidden="true" />
                    <span>{errorMessage}</span>
                </p>
            )}
            <AuthField
                id={FIELD_IDS.email}
                label="Email address"
                type="email"
                autoComplete="username"
                value={email}
                onChange={setEmail}
                autoFocus={!email}
                invalid={badFields.includes('email')}
                describedBy={errorMessage ? 'auth-error' : undefined}
            />
            <AuthField
                id={FIELD_IDS.password}
                label="Password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={setPassword}
                autoFocus={Boolean(email)}
                invalid={badFields.includes('password')}
                describedBy={errorMessage ? 'auth-error' : undefined}
                labelAction={(
                    <button type="button" className="auth-link" onClick={handleForgot} disabled={pending}>
                        Forgot password?
                    </button>
                )}
            />
            <button type="submit" className="auth-btn top" disabled={pending || !password}>
                {pending ? 'Signing in…' : 'Sign in'}
            </button>
        </form>
    );
};

const ResetSent: React.FC<{ email: string; onBack: () => void }> = ({ email, onBack }) => {
    const resend = async () => {
        try {
            await passwordAuth.requestPasswordReset(email);
        } catch {
            // Same page whatever happens, so nothing about the address leaks.
        }
    };
    return (
        <div className="auth-center">
            <span className="auth-bigicon"><MailIcon aria-hidden="true" /></span>
            <h2>Check your email</h2>
            <p className="auth-note">
                If {email} has an account, we've sent a link to choose a new password. It works for one hour.
            </p>
            <button type="button" className="auth-btn top" onClick={onBack}>Back to sign in</button>
            <p className="auth-note">
                Nothing after a few minutes? Check spam, or{' '}
                <button type="button" className="auth-link" onClick={resend}>send it again</button>.
            </p>
        </div>
    );
};

const LoginPage: React.FC<LoginPageProps> = ({ authEnabled, authReady, displayName, onSignIn, onAuthenticated }) => {
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [localUser, setLocalUser] = useState('');
    const [sentTo, setSentTo] = useState('');
    const [lastEmail, setLastEmail] = useState('');
    const navigate = useNavigate();
    const passwordMode = isPasswordMode();

    const handleSignIn = async () => {
        setErrorMessage('');
        setPending(true);
        try {
            if (!authEnabled) {
                // local dev auth: set X-User-ID header
                const userId = localUser.trim() || 'local-user';
                setUserId(userId);
                navigate('/');
            } else {
                await onSignIn();
            }
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Sign in failed.');
        } finally {
            setPending(false);
        }
    };

    if (passwordMode && sentTo) {
        return (
            <AuthShell title="Check your email" bare>
                <ResetSent email={sentTo} onBack={() => { setLastEmail(sentTo); setSentTo(''); }} />
            </AuthShell>
        );
    }

    return (
        <AuthShell title="Welcome back" subtitle="Sign in to your photos and videos.">
            {passwordMode ? (
                <>
                    {authReady && displayName && <p className="auth-note ok">You are signed in as {displayName}.</p>}
                    <PasswordLogin initialEmail={lastEmail} onAuthenticated={onAuthenticated} onResetSent={setSentTo} />
                    <div className="auth-foot">Invited by someone? Open the link in your invite email.</div>
                </>
            ) : !authEnabled ? (
                <>
                    <p className="auth-note">Authentication is currently disabled for this deployment. Use local sign-in below.</p>
                    <div className="auth-form">
                        <AuthField id="local-user" label="Username" value={localUser} onChange={setLocalUser} />
                        <button type="button" className="auth-btn top" onClick={handleSignIn} disabled={pending}>
                            Continue
                        </button>
                    </div>
                </>
            ) : (
                <>
                    {!authReady && <p className="auth-note">Preparing sign-in flow…</p>}
                    {authReady && displayName && <p className="auth-note ok">You are signed in as {displayName}.</p>}
                    {authReady && !displayName && <p className="auth-note">Sign in with your Microsoft account to continue to your photo library.</p>}
                    {errorMessage && (
                        <p className="auth-msg box" role="alert"><AlertIcon aria-hidden="true" /><span>{errorMessage}</span></p>
                    )}
                    <div className="auth-form">
                        <button type="button" className="auth-btn top" onClick={handleSignIn} disabled={!authReady || pending}>
                            <ArrowRightOnRectangleIcon aria-hidden="true" width={18} height={18} />
                            <span>{pending ? 'Signing in…' : displayName ? 'Use a different account' : 'Sign in with Microsoft'}</span>
                        </button>
                        <Link className="auth-link quiet auth-back" to="/">
                            <ArrowLeftIcon aria-hidden="true" />
                            Back to gallery
                        </Link>
                    </div>
                </>
            )}
        </AuthShell>
    );
};

export default LoginPage;
