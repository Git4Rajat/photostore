import React, { useEffect, useRef, useState } from 'react';
import { StarIcon as StarSolid } from '@heroicons/react/24/solid';
import { StarIcon as StarOutline } from '@heroicons/react/24/outline';
import type { SwatchKey } from '../types';

/** A placeholder photo swatch (stands in for a real thumbnail). */
export const Swatch: React.FC<{ swatch: SwatchKey; className?: string }> = ({ swatch, className }) => (
    <span className={`mock-swatch ${swatch}${className ? ` ${className}` : ''}`} aria-hidden="true" />
);

/** Initials avatar — reused for people, members, and the account menu. */
export const Avatar: React.FC<{ initials: string; color?: string; size?: number; className?: string }> = ({
    initials,
    color,
    size = 40,
    className,
}) => (
    <span
        className={`pt-avatar${className ? ` ${className}` : ''}`}
        style={{ width: size, height: size, background: color || undefined, fontSize: size * 0.34 }}
        aria-hidden="true"
    >
        {initials}
    </span>
);

/** Interactive 5-star rating. Clicking the current rating again clears it.
 *
 * Renders both the full 5-star row and a compact single partial-fill star --
 * CSS (see `.pt-stars-compact`/`.pt-stars-full`) swaps which one is visible
 * below the width where 5 separate star buttons crowd into a neighboring
 * button (e.g. the lightbox action bar's Like button on mobile). The compact
 * star cycles the rating up by one per tap (wrapping past 5 back to 0),
 * mirroring the full row's "tap current value again to clear" behavior. */
export const Stars: React.FC<{ value: number; onRate?: (n: number) => void; size?: number }> = ({
    value,
    onRate,
    size = 18,
}) => {
    const [hover, setHover] = useState<number>(0);
    const shown = hover || value;
    return (
        <>
            <span className="pt-stars pt-stars-full" onMouseLeave={() => setHover(0)}>
                {[1, 2, 3, 4, 5].map((n) => {
                    const Filled = n <= shown;
                    const Icon = Filled ? StarSolid : StarOutline;
                    return (
                        <button
                            key={n}
                            type="button"
                            className={`pt-star${onRate ? '' : ' static'}`}
                            aria-label={`${n} star${n > 1 ? 's' : ''}`}
                            onMouseEnter={() => onRate && setHover(n)}
                            onClick={(e) => {
                                e.stopPropagation();
                                onRate?.(value === n ? 0 : n);
                            }}
                            disabled={!onRate}
                            style={{ width: size, height: size }}
                        >
                            <Icon />
                        </button>
                    );
                })}
            </span>
            <button
                type="button"
                className={`pt-star-compact${onRate ? '' : ' static'}`}
                aria-label={`Rating: ${value} of 5 stars`}
                onClick={(e) => {
                    e.stopPropagation();
                    onRate?.(value >= 5 ? 0 : value + 1);
                }}
                disabled={!onRate}
                style={{ width: size, height: size, '--pt-star-fill': `${Math.max(0, Math.min(5, value)) / 5 * 100}%` } as React.CSSProperties}
            >
                <StarOutline className="pt-star-compact-bg" />
                <StarSolid className="pt-star-compact-fg" />
            </button>
        </>
    );
};

/** Lightweight popover menu. Caller renders the trigger; panel closes on
 *  outside-click / Escape. */
export const Menu: React.FC<{
    renderTrigger: (toggle: () => void, open: boolean) => React.ReactNode;
    children: (close: () => void) => React.ReactNode;
    align?: 'left' | 'right';
    className?: string;
}> = ({ renderTrigger, children, align = 'left', className }) => {
    const [open, setOpen] = useState(false);
    const wrapRef = useRef<HTMLDivElement | null>(null);
    const toggle = () => setOpen((v) => !v);
    const close = () => setOpen(false);

    useEffect(() => {
        if (!open) return undefined;
        const onDown = (e: MouseEvent) => {
            if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) close();
        };
        const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close();
        document.addEventListener('mousedown', onDown);
        document.addEventListener('keydown', onKey);
        return () => {
            document.removeEventListener('mousedown', onDown);
            document.removeEventListener('keydown', onKey);
        };
    }, [open]);

    return (
        <div className={`pt-menu-anchor${className ? ` ${className}` : ''}`} ref={wrapRef}>
            {renderTrigger(toggle, open)}
            {open && <div className={`pt-menu-panel ${align}`}>{children(close)}</div>}
        </div>
    );
};
