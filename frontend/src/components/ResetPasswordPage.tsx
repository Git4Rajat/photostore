import React, { useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import * as passwordAuth from '../services/passwordAuthClient';

// Public page reached from the "reset your password" link in a recovery email:
//   /reset-password?token=<token>
const ResetPasswordPage: React.FC = () => {
    const [searchParams] = useSearchParams();
    const token = searchParams.get('token') || '';
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
            setErrorMessage('Password must be at least 8 characters.');
            return;
        }
        if (newPassword !== confirm) {
            setErrorMessage('Passwords do not match.');
            return;
        }
        setPending(true);
        try {
            await passwordAuth.resetPassword(token, newPassword);
            setDone(true);
        } catch (error) {
            setErrorMessage(error instanceof Error ? error.message : 'Could not reset password.');
        } finally {
            setPending(false);
        }
    };

    return (
        <section className="auth-page card-glass">
            <p className="additional-kicker">RESET</p>
            <h2 className="auth-page-title">Choose a new password</h2>
            {!token ? (
                <p className="status error">This link is missing its reset token. Request a new reset email from the login page.</p>
            ) : done ? (
                <>
                    <p className="status success">Your password has been reset. You can now sign in.</p>
                    <button type="button" className="btn btn-primary" onClick={() => navigate('/login')}>
                        Go to sign in
                    </button>
                </>
            ) : (
                <form className="local-login" onSubmit={handleSubmit}>
                    <label htmlFor="new-password">New password</label>
                    <input
                        id="new-password"
                        type="password"
                        autoComplete="new-password"
                        value={newPassword}
                        onChange={(e) => setNewPassword(e.target.value)}
                    />
                    <label htmlFor="confirm-password">Confirm new password</label>
                    <input
                        id="confirm-password"
                        type="password"
                        autoComplete="new-password"
                        value={confirm}
                        onChange={(e) => setConfirm(e.target.value)}
                    />
                    {errorMessage && <p className="status error">{errorMessage}</p>}
                    <button type="submit" className="btn btn-primary" disabled={pending || !newPassword || !confirm}>
                        {pending ? 'Resetting…' : 'Reset password'}
                    </button>
                    <Link className="btn btn-link auth-page-link" to="/login">Back to sign in</Link>
                </form>
            )}
        </section>
    );
};

export default ResetPasswordPage;
