import React, { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import * as passwordAuth from '../services/passwordAuthClient';
import { Check as CheckIcon, Plus as PlusIcon, TriangleAlert as AlertIcon } from 'lucide-react';
import AuthShell from './shared/AuthShell';
import AuthField from './shared/AuthField';
import { MIN_PASSWORD_LENGTH } from './shared/passwordRules';

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
    // Hard navigation: this page can render under a router that only knows
    // the /reset-password route (see PrototypeApp).
    const goToSignIn = () => { window.location.href = '/login'; };

    const handleSubmit = async (event: React.FormEvent) => {
        event.preventDefault();
        setErrorMessage('');
        if (newPassword.length < MIN_PASSWORD_LENGTH) {
            setErrorMessage(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
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

    const longEnough = newPassword.length >= MIN_PASSWORD_LENGTH;
    const matches = confirm.length > 0 && newPassword === confirm;

    return (
        <AuthShell title="Choose a new password" subtitle="You'll use it the next time you sign in.">
            {!token ? (
                <p className="auth-msg box" role="alert"><AlertIcon aria-hidden="true" /><span>This link is missing its reset token. Request a new reset email from the sign-in page.</span></p>
            ) : done ? (
                <div className="auth-center">
                    <p className="auth-note ok">Your password has been reset. You can now sign in.</p>
                    <button type="button" className="auth-btn top" onClick={goToSignIn}>Go to sign in</button>
                </div>
            ) : (
                <form className="auth-form" onSubmit={handleSubmit} noValidate>
                    {errorMessage && <p className="auth-msg box" role="alert"><AlertIcon aria-hidden="true" /><span>{errorMessage}</span></p>}
                    <AuthField id="new-password" label="New password" type="password" autoComplete="new-password" value={newPassword} onChange={setNewPassword} />
                    <AuthField id="confirm-password" label="Confirm new password" type="password" autoComplete="new-password" value={confirm} onChange={setConfirm} />
                    <ul className="auth-rules" aria-label="Password rules">
                        <li className={longEnough ? 'ok' : undefined}>{longEnough ? <CheckIcon aria-hidden="true" /> : <PlusIcon aria-hidden="true" />}At least {MIN_PASSWORD_LENGTH} characters</li>
                        <li className={matches ? 'ok' : undefined}>{matches ? <CheckIcon aria-hidden="true" /> : <PlusIcon aria-hidden="true" />}Both passwords match</li>
                    </ul>
                    <button type="submit" className="auth-btn" disabled={pending || !longEnough || !matches}>
                        {pending ? 'Resetting…' : 'Reset password'}
                    </button>
                    <button type="button" className="auth-link quiet auth-back" onClick={goToSignIn}>Back to sign in</button>
                </form>
            )}
        </AuthShell>
    );
};

export default ResetPasswordPage;
