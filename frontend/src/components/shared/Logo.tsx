import React from 'react';

interface LogoProps {
    size?: number;
    className?: string;
    title?: string;
}

// The Keepsake mark: a photo card with a heart (a "kept memory") on a
// brand-blue badge. Mirrors /favicon.svg. Colors are fixed brand colors
// (not currentColor) so the mark reads identically in light and dark themes.
// A per-instance gradient id keeps multiple inline copies from colliding.
export const Logo: React.FC<LogoProps> = ({ size = 28, className, title = 'Keepsake' }) => {
    const gid = React.useId();
    return (
        <svg
            className={className}
            width={size}
            height={size}
            viewBox="0 0 64 64"
            role="img"
            aria-label={title}
            xmlns="http://www.w3.org/2000/svg"
        >
            <defs>
                <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0" stopColor="#3b82f6" />
                    <stop offset="1" stopColor="#1450c0" />
                </linearGradient>
            </defs>
            <rect width="64" height="64" rx="15" fill={`url(#${gid})`} />
            <rect x="21" y="15" width="26" height="26" rx="6" fill="#ffffff" opacity="0.32" transform="rotate(9 34 28)" />
            <rect x="16" y="18" width="32" height="32" rx="6" fill="#ffffff" />
            <path
                d="M32 41c-6-4.5-10.5-8-10.5-12 0-2.7 2-4.5 4.3-4.5 2.1 0 4.1 1.3 6.2 3.9 2.1-2.6 4.1-3.9 6.2-3.9 2.3 0 4.3 1.8 4.3 4.5 0 4-4.5 7.5-10.5 12z"
                fill="#1e6ae1"
            />
        </svg>
    );
};

interface LogoLockupProps {
    size?: number;
    className?: string;
    tagline?: boolean;
    subtitle?: string;
}

// Mark + "Keepsake" wordmark, with an optional tagline underneath. Used on the
// login screen and anywhere a full brand lockup is warranted.
export const LogoLockup: React.FC<LogoLockupProps> = ({
    size = 40,
    className,
    tagline = false,
    subtitle = 'An elegant home for your memories.',
}) => (
    <div className={`logo-lockup${className ? ` ${className}` : ''}`}>
        <Logo size={size} />
        <span className="logo-lockup-text">
            <span className="logo-wordmark">Keepsake</span>
            {tagline && <span className="logo-tagline">{subtitle}</span>}
        </span>
    </div>
);

export default Logo;
