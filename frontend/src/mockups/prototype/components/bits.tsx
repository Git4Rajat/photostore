import React, { useEffect, useRef, useState } from 'react';
import { Star, X as XMarkIcon } from 'lucide-react';
import type { SwatchKey } from '../types';

/** Shared floating multi-select action bar — one consistent bottom-center pill
 *  for "N selected → act" across photos, people, and albums (replacing the mix
 *  of a floating bar, a different floating menu, and an inline list bar). It
 *  always carries a clear/close button and the selection count; callers pass
 *  their own action buttons/menus as children. */
export const SelectionBar: React.FC<{
    count: number;
    onClear: () => void;
    label?: string;
    children?: React.ReactNode;
}> = ({ count, onClear, label = 'Selection actions', children }) => (
    <div className="pt-floating-menu" role="toolbar" aria-label={label}>
        <button type="button" className="pt-fm-close" onClick={onClear} aria-label="Clear selection">
            <XMarkIcon />
        </button>
        <div className="pt-fm-badge">{count}</div>
        {children}
    </div>
);

/** Shared inline loading indicator — one consistent spinner + label for every
 *  in-page "Loading…" state (grids, lists, the viewer), replacing the mix of
 *  ad-hoc plain-text placeholders. */
export const Spinner: React.FC<{ label?: string; center?: boolean; className?: string }> = ({ label = 'Loading…', center = true, className }) => (
    <div className={`pt-loading${center ? ' center' : ''}${className ? ` ${className}` : ''}`} role="status" aria-live="polite">
        <span className="pt-spinner" aria-hidden="true" />
        {label && <span>{label}</span>}
    </div>
);

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
                    const filled = n <= shown;
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
                        >
                            <Star fill={filled ? 'currentColor' : 'none'} style={{ width: size, height: size }} />
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
                style={{ '--pt-star-fill': `${Math.max(0, Math.min(5, value)) / 5 * 100}%` } as React.CSSProperties}
            >
                <Star className="pt-star-compact-bg" />
                <Star className="pt-star-compact-fg" fill="currentColor" />
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
    const panelRef = useRef<HTMLDivElement | null>(null);
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

    // Move focus into the panel when it opens so the menu is operable by keyboard
    // (and screen readers announce it), matching HIG menu behavior.
    useEffect(() => {
        if (!open) return;
        panelRef.current?.querySelector<HTMLElement>('button:not([disabled]), [href], input, select')?.focus();
    }, [open]);

    // Arrow keys roam between the menu's own controls; Home/End jump to ends.
    const onPanelKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
        const items = Array.from(
            panelRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input, select') ?? [],
        );
        if (!items.length) return;
        e.preventDefault();
        const current = items.indexOf(document.activeElement as HTMLElement);
        let next = current;
        if (e.key === 'ArrowDown') next = current < items.length - 1 ? current + 1 : 0;
        else if (e.key === 'ArrowUp') next = current > 0 ? current - 1 : items.length - 1;
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = items.length - 1;
        items[next]?.focus();
    };

    return (
        <div className={`pt-menu-anchor${className ? ` ${className}` : ''}`} ref={wrapRef}>
            {renderTrigger(toggle, open)}
            {open && (
                <div ref={panelRef} className={`pt-menu-panel ${align}`} role="menu" onKeyDown={onPanelKeyDown}>
                    {children(close)}
                </div>
            )}
        </div>
    );
};
