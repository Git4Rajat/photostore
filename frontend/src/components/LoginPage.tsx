import React, { useState } from 'react';
import { ArrowLeftIcon, ArrowRightOnRectangleIcon } from '@heroicons/react/24/outline';
import { Link, useNavigate } from 'react-router-dom';
import { setUserId } from '../services/apiClient';

interface LoginPageProps {
    authEnabled: boolean;
    authReady: boolean;
    displayName: string;
    onSignIn: () => Promise<void>;
}

const LoginPage: React.FC<LoginPageProps> = ({ authEnabled, authReady, displayName, onSignIn }) => {
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [localUser, setLocalUser] = useState('');
    const navigate = useNavigate();

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
            {!authEnabled ? (
                <>
                    <p className="status">Authentication is currently disabled for this deployment. Use local sign-in below.</p>
                    <div className="local-login">
                        <label htmlFor="local-user">Username</label>
                        <input id="local-user" type="text" value={localUser} onChange={(e) => setLocalUser(e.target.value)} />
                        <button type="button" className="btn btn-primary" onClick={handleSignIn} disabled={pending}>
                            Continue
                        </button>
                    </div>
                </>
            ) : (
                <>
                    {!authReady && <p className="status">Preparing sign-in flow...</p>}
                    {authReady && displayName && (
                        <p className="status success">You are signed in as {displayName}.</p>
                    )}
                    {authReady && !displayName && <p className="status">Sign in to continue to your photo library.</p>}
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    <div className="auth-page-actions">
                        <button type="button" className="btn btn-primary icon-btn" onClick={handleSignIn} disabled={!authReady || pending} aria-label="Sign in with Microsoft">
                            <ArrowRightOnRectangleIcon className="toolbar-icon" />
                            <span className="sr-only">{pending ? 'Signing in' : 'Sign in with Microsoft'}</span>
                        </button>
                        <Link className="btn btn-soft icon-btn auth-page-link" to="/" aria-label="Back to gallery">
                            <ArrowLeftIcon className="toolbar-icon" />
                            <span className="sr-only">Back to gallery</span>
                        </Link>
                    </div>
                </>
            )}
        </section>
    );
};

export default LoginPage;
