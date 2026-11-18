import React from 'react';

interface LogoProps {
    size?: number;
    className?: string;
    title?: string;
}

// The Keepsake mark from the redesign: a small solid heart (a "kept memory")
// on an accent-coloured rounded square. Colours come from --accent /
// --on-accent, so it follows the light and dark themes like the rest of the
// UI. Mirrors /favicon.svg; the box and glyph keep the mock's 22px/7px and
// 48px/15px proportions at any size.
export const Logo: React.FC<LogoProps> = ({ size = 28, className, title = 'Keepsake' }) => (
    <span
        className={`ks-mark${className ? ` ${className}` : ''}`}
        style={{ width: size, height: size, borderRadius: Math.round(size * 0.31) }}
        role="img"
        aria-label={title}
    >
        <svg width={size * 0.56} height={size * 0.56} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M12 19.5s-7-4.3-7-9.4A3.9 3.9 0 0 1 12 7.8a3.9 3.9 0 0 1 7 2.3c0 5.1-7 9.4-7 9.4z" />
        </svg>
    </span>
);

interface LogoLockupProps {
    size?: number;
    className?: string;
    tagline?: boolean;
    subtitle?: string;
}

// Mark + "Keepsake" wordmark, with an optional tagline underneath. Used in the
// app and shared-album headers and anywhere a full brand lockup is warranted.
export const LogoLockup: React.FC<LogoLockupProps> = ({
    size = 28,
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
