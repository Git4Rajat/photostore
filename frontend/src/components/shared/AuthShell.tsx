import React, { useEffect } from 'react';
import { Logo, LogoLockup } from './Logo';
import './AuthShell.css';

interface AuthShellProps {
    title: string;
    // One calm line under the title (the mock-up's "Sign in to your photos and videos.").
    subtitle?: string;
    // Confirmation screens: the mock-up shows only the centred message (which carries its own heading).
    bare?: boolean;
    children: React.ReactNode;
}

// Tones for the desktop picture panel. The sign-in page is signed out, so it cannot show the
// library's own photos; soft blocks in the theme's colours stand in for the mock-up's photo wall.
const ART_TILES = ['a s2', 'b', 'c', 'd s2', 'e', 'f', 'g s2', 'h', 'i'];

// Shared full-viewport layout for the signed-out auth screens: sign-in, accept invite, reset
// password and confirm-cleanup. Phones get the mock-up's single column (mark, title, form); from
// 900px the left half shows pictures and the form keeps its own width on the right.
const AuthShell: React.FC<AuthShellProps> = ({ title, subtitle, bare = false, children }) => {
    // Let the screen run under the notch / status bar and the browser's bottom bar (its padding
    // already honours the safe-area insets). The signed-in app keeps its own viewport.
    useEffect(() => {
        const meta = document.querySelector<HTMLMetaElement>('meta[name="viewport"]');
        const original = meta?.getAttribute('content') ?? null;
        if (meta && original && !/viewport-fit/.test(original)) meta.setAttribute('content', `${original}, viewport-fit=cover`);
        return () => { if (meta && original !== null) meta.setAttribute('content', original); };
    }, []);
    return (
    <div className="auth-screen">
        <aside className="auth-art" aria-hidden="true">
            <div className="auth-art-grid">
                {ART_TILES.map((tile) => <span key={tile} className={`auth-art-tile ${tile}`} />)}
            </div>
            <div className="auth-art-caption">
                <b>Every photo you've kept, in one private place.</b>
                <span>Yours and your family's. Nobody else can see it.</span>
            </div>
        </aside>
        <main className="auth-pane" role="main">
            <div className="auth-box">
                <div className={`auth-brand${bare ? ' bare' : ''}`}>
                    {!bare && <Logo size={48} className="auth-brand-mark" />}
                    <LogoLockup size={22} className="auth-brand-lockup" />
                    {!bare && <h1 className="auth-title">{title}</h1>}
                    {!bare && subtitle && <p className="auth-subtitle">{subtitle}</p>}
                </div>
                {children}
            </div>
        </main>
    </div>
    );
};

export default AuthShell;
