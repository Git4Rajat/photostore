import React from 'react';
import { Link } from 'react-router-dom';
import { LogoLockup } from './shared/Logo';

// Branded catch-all for unknown routes.
const NotFoundPage: React.FC = () => (
    <section className="auth-page card-glass">
        <LogoLockup size={46} className="auth-logo" />
        <p className="additional-kicker">404</p>
        <h2 className="auth-page-title">This page wandered off</h2>
        <p className="status">
            We couldn&apos;t find what you were looking for. It may have been moved, or the link isn&apos;t quite right.
        </p>
        <Link className="btn btn-primary auth-page-link" to="/">
            Back to your gallery
        </Link>
    </section>
);

export default NotFoundPage;
