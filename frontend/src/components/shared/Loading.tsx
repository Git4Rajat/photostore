import React from 'react';
import { Logo } from './Logo';

interface LoadingProps {
    label?: string;
    // Page-level loaders reserve vertical space so the layout doesn't jump.
    fullPage?: boolean;
}

// Branded loading indicator: the Keepsake mark inside a spinning accent ring.
// Used for route/Suspense fallbacks in place of plain "Loading…" text.
export const Loading: React.FC<LoadingProps> = ({ label = 'Loading…', fullPage = true }) => (
    <div className={`loading-state${fullPage ? ' loading-state--page' : ''}`} role="status" aria-live="polite">
        <span className="loading-mark">
            <Logo size={40} />
            <span className="loading-spinner" aria-hidden="true" />
        </span>
        {label && <span className="loading-label">{label}</span>}
    </div>
);

export default Loading;
