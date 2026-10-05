import React from 'react';
import { LogoLockup } from './Logo';

interface AuthShellProps {
    // Short mono/uppercase label above the title (e.g. "ACCESS", "INVITATION").
    kicker: string;
    title: string;
    // Show the brand tagline under the wordmark (used on the sign-in screen).
    tagline?: boolean;
    children: React.ReactNode;
}

// Shared full-viewport layout for the signed-out auth screens — sign-in, accept
// invite, reset password and confirm-cleanup. Centers a glass card over the
// app's warm "Light Table" backdrop so these pages read as the same product as
// the main prototype UI instead of a bare browser form floating at the top of
// the page.
const AuthShell: React.FC<AuthShellProps> = ({ kicker, title, tagline = false, children }) => (
    <div className="auth-screen">
        <div className="auth-screen-backdrop" aria-hidden="true">
            <span className="auth-blob auth-blob-1" />
            <span className="auth-blob auth-blob-2" />
        </div>
        <main className="auth-page card-glass" role="main">
            <LogoLockup size={44} tagline={tagline} className="auth-logo" />
            <p className="additional-kicker auth-kicker">{kicker}</p>
            <h1 className="auth-page-title">{title}</h1>
            {children}
        </main>
    </div>
);

export default AuthShell;
