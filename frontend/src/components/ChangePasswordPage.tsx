import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import * as passwordAuth from '../services/passwordAuthClient';

// In-app password change for the single owner (AUTH_MODE=password), reached at
// /change-password while signed in.
const ChangePasswordPage: React.FC = () => {
    const [currentPassword, setCurrentPassword] = useState('');
    const [newPassword, setNewPassword] = useState('');
    const [confirm, setConfirm] = useState('');
    const [pending, setPending] = useState(false);
    const [errorMessage, setErrorMessage] = useState('');
    const [done, setDone] = useState(false);
    const navigate = useNavigate();

    const handleSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        setErrorMessage('');
        if (newPassword.length < 8) {
            setErrorMessage('New password must be at least 8 characters.');
            return;
        }
        if (newPassword !== confirm) {
            setErrorMessage('Passwords do not match.');
            return;
        }
        setPending(true);
        try {
            await passwordAuth.changePassword(currentPassword, newPassword);
            setDone(true);
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Could not change password.');
        } finally {
            setPending(false);
        }
    };

    return (
        <section className="auth-page card-glass">
            <p className="additional-kicker">ACCOUNT</p>
            <h2 className="auth-page-title">Change your password</h2>
            {done ? (
                <>
                    <p className="status success">Your password has been changed.</p>
                    <button type="button" className="btn btn-primary" onClick={() => navigate('/')}>
                        Back to gallery
                    </button>
                </>
            ) : (
                <form className="local-login" onSubmit={handleSubmit}>
                    <label htmlFor="current-password">Current password</label>
                    <input
                        id="current-password"
                        className="field"
                        type="password"
                        autoComplete="current-password"
                        value={currentPassword}
                        onChange={(e) => setCurrentPassword(e.target.value)}
                    />
                    <label htmlFor="new-password">New password</label>
                    <input
                        id="new-password"
                        className="field"
                        type="password"
                        autoComplete="new-password"
                        value={newPassword}
                        onChange={(e) => setNewPassword(e.target.value)}
                    />
                    <label htmlFor="confirm-password">Confirm new password</label>
                    <input
                        id="confirm-password"
                        className="field"
                        type="password"
                        autoComplete="new-password"
                        value={confirm}
                        onChange={(e) => setConfirm(e.target.value)}
                    />
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    <button
                        type="submit"
                        className="btn btn-primary"
                        disabled={pending || !currentPassword || !newPassword || !confirm}
                    >
                        {pending ? 'Saving…' : 'Change password'}
                    </button>
                    <Link className="btn btn-link auth-page-link" to="/">Back to gallery</Link>
                </form>
            )}
        </section>
    );
};

export default ChangePasswordPage;
