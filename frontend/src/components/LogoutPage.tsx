import React, { useState } from 'react';
import { ArrowLeftIcon, ArrowLeftOnRectangleIcon } from '@heroicons/react/24/outline';
import { Link, useNavigate } from 'react-router-dom';
import { setUserId } from '../services/apiClient';

interface LogoutPageProps {
    authEnabled: boolean;
    authReady: boolean;
    displayName: string;
    onSignOut: () => Promise<void>;
}

const LogoutPage: React.FC<LogoutPageProps> = ({ authEnabled, authReady, displayName, onSignOut }) => {
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const navigate = useNavigate();

    const handleSignOut = async () => {
        setErrorMessage('');
        setPending(true);
        try {
            if (!authEnabled) {
                // local dev logout
                setUserId(null);
                navigate('/');
            } else {
                await onSignOut();
            }
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Sign out failed.');
        } finally {
            setPending(false);
        }
    };

    return (
        <section className="auth-page card-glass">
            <p className="additional-kicker">ACCESS</p>
            <h2 className="auth-page-title">Logout</h2>
            {!authEnabled ? (
                <p className="status">Authentication is currently disabled for this deployment.</p>
            ) : (
                <>
                    {!authReady && <p className="status">Preparing sign-out flow...</p>}
                    {authReady && displayName && (
                        <p className="status">Signed in as {displayName}. Use the button below to sign out.</p>
                    )}
                    {authReady && !displayName && <p className="status success">You are already signed out.</p>}
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    <div className="auth-page-actions">
                        <button
                            type="button"
                            className="btn btn-danger icon-btn"
                            onClick={handleSignOut}
                            disabled={!authReady || pending || !displayName}
                            aria-label="Sign out"
                        >
                            <ArrowLeftOnRectangleIcon className="toolbar-icon" />
                            <span className="sr-only">{pending ? 'Signing out' : 'Sign out'}</span>
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

export default LogoutPage;
