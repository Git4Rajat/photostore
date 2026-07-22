import React, { useState } from 'react';
import { ArrowLeftIcon, ArrowRightOnRectangleIcon } from '@heroicons/react/24/outline';
import { Link, useNavigate } from 'react-router-dom';
import { setUserId } from '../services/apiClient';
import { getRuntimeConfig } from '../config/appConfig';
import * as passwordAuth from '../services/passwordAuthClient';

interface LoginPageProps {
    authEnabled: boolean;
    authReady: boolean;
    displayName: string;
    onSignIn: () => Promise<void>;
    // Called after a successful password login so the app can refresh auth state.
    onAuthenticated?: () => Promise<void>;
}

const isPasswordMode = (): boolean => (getRuntimeConfig().authMode || '').toLowerCase() === 'password';

const PasswordLogin: React.FC<{ onAuthenticated?: () => Promise<void> }> = ({ onAuthenticated }) => {
    const [email, setEmail] = useState(() => passwordAuth.getEmailHint());
    const [password, setPassword] = useState('');
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [resetSent, setResetSent] = useState(false);
    const navigate = useNavigate();

    const handleLogin = async (event: React.FormEvent) => {
        event.preventDefault();
        setErrorMessage('');
        setPending(true);
        try {
            await passwordAuth.login(email.trim(), password);
            setPassword('');
            if (onAuthenticated) {
                await onAuthenticated();
            }
            navigate('/');
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Sign in failed.');
        } finally {
            setPending(false);
        }
    };

    const handleForgot = async () => {
        setErrorMessage('');
        try {
            await passwordAuth.requestPasswordReset();
        } finally {
            // Always show the same confirmation regardless of outcome.
            setResetSent(true);
        }
    };

    return (
        <form className="local-login" onSubmit={handleLogin}>
            <label htmlFor="owner-email">Email</label>
            <input
                id="owner-email"
                className="field"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoFocus={!email}
            />
            <label htmlFor="owner-password">Password</label>
            <input
                id="owner-password"
                className="field"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoFocus={Boolean(email)}
            />
            {errorMessage && <p className="status error">{errorMessage}</p>}
            {resetSent && (
                <p className="status success">
                    If password recovery is set up and an email is on file, a reset link is on its way.
                    Check your inbox (and spam folder).
                </p>
            )}
            <button type="submit" className="btn btn-primary" disabled={pending || !password}>
                {pending ? 'Signing in…' : 'Sign in'}
            </button>
            <button type="button" className="btn btn-link auth-page-link" onClick={handleForgot} disabled={pending}>
                Forgot password?
            </button>
        </form>
    );
};

const LoginPage: React.FC<LoginPageProps> = ({ authEnabled, authReady, displayName, onSignIn, onAuthenticated }) => {
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [localUser, setLocalUser] = useState('');
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

    return (
        <section className="auth-page card-glass">
            <p className="additional-kicker">ACCESS</p>
            <h2 className="auth-page-title">Login</h2>
            {passwordMode ? (
                <>
                    {authReady && displayName ? (
                        <p className="status success">You are signed in as {displayName}.</p>
                    ) : (
                        <p className="status">Enter your password to continue to your photo library.</p>
                    )}
                    <PasswordLogin onAuthenticated={onAuthenticated} />
                    <Link className="btn btn-soft auth-page-link auth-page-back" to="/">
                        <ArrowLeftIcon className="toolbar-icon" />
                        <span>Back to gallery</span>
                    </Link>
                </>
            ) : !authEnabled ? (
                <>
                    <p className="status">Authentication is currently disabled for this deployment. Use local sign-in below.</p>
                    <div className="local-login">
                        <label htmlFor="local-user">Username</label>
                        <input id="local-user" className="field" type="text" value={localUser} onChange={(e) => setLocalUser(e.target.value)} />
                        <button type="button" className="btn btn-primary" onClick={handleSignIn} disabled={pending}>
                            Continue
                        </button>
                    </div>
                </>
            ) : (
                <>
                    {!authReady && <p className="status">Preparing sign-in flow…</p>}
                    {authReady && displayName && (
                        <p className="status success">You are signed in as {displayName}.</p>
                    )}
                    {authReady && !displayName && <p className="status">Sign in with your Microsoft account to continue to your photo library.</p>}
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    <div className="auth-page-actions">
                        <button type="button" className="btn btn-primary" onClick={handleSignIn} disabled={!authReady || pending}>
                            <ArrowRightOnRectangleIcon className="toolbar-icon" />
                            <span>{pending ? 'Signing in…' : displayName ? 'Use a different account' : 'Sign in with Microsoft'}</span>
                        </button>
                        <Link className="btn btn-soft auth-page-link" to="/">
                            <ArrowLeftIcon className="toolbar-icon" />
                            <span>Back to gallery</span>
                        </Link>
                    </div>
                </>
            )}
        </section>
    );
};

export default LoginPage;
