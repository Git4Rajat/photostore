import React, { useEffect, useRef, useState } from 'react';
import { Check, ListChecks } from 'lucide-react';

// Same thresholds as ScrollToTopButton: eligible once scrolled this far, and
// only revealed after scrolling has settled so it never sits over content
// mid-gesture.
const SHOW_AFTER_PX = 400;
const SETTLE_MS = 150;

/**
 * Floating "Select" / "Done" toggle for pages with a photo grid. The page's own
 * Select button lives in its top toolbar and scrolls out of view, so a user deep in
 * a long grid had to scroll all the way back up just to start (or leave) selecting.
 * It sits beside the scroll-to-top button and shares its scroll-container/settle
 * behaviour. Select state belongs to the host page (the store's global select mode in
 * the app, local state on the public album page), so it is passed in.
 *
 * `grid` names the grid this button selects from, so the photo button never shows over
 * the People grid (where it sat on top of People's own selection bar and hid Merge).
 * `hideOnTouch`: on touch screens a long-press on a tile starts selecting, so the pill
 * is only clutter there; pages without long-press (the public album) leave it on.
 */
export const FloatingSelectButton: React.FC<{
    selectMode: boolean;
    onToggle: () => void;
    suppressed?: boolean;
    noun?: string;
    grid?: string;
    hideOnTouch?: boolean;
}> = ({ selectMode, onToggle, suppressed = false, noun = 'photos', grid = '.pt-grid', hideOnTouch = false }) => {
    const [visible, setVisible] = useState(false);
    const settleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

    useEffect(() => {
        const container = document.querySelector<HTMLElement>('.pt-body');
        if (!container) {
            return undefined;
        }
        const onScroll = () => {
            if (settleTimer.current) {
                clearTimeout(settleTimer.current);
            }
            setVisible(false);
            if (container.scrollTop <= SHOW_AFTER_PX) {
                return;
            }
            // Only offer it where there is something to select on the current page.
            settleTimer.current = setTimeout(() => setVisible(Boolean(container.querySelector(grid))), SETTLE_MS);
        };
        container.addEventListener('scroll', onScroll, { passive: true });
        return () => {
            container.removeEventListener('scroll', onScroll);
            if (settleTimer.current) {
                clearTimeout(settleTimer.current);
            }
        };
    }, [grid]);

    // `suppressed`: another control already occupies this corner (the app's bottom selection bar,
    // which has its own close button, covers it once something is selected).
    const shown = visible && !suppressed;
    const label = selectMode ? 'Done selecting' : `Select ${noun}`;
    return (
        <button
            type="button"
            className={`pt-float-select${shown ? ' visible' : ''}${selectMode ? ' active' : ''}${hideOnTouch ? ' touch-hidden' : ''}`}
            onClick={onToggle}
            aria-label={label}
            aria-pressed={selectMode}
            tabIndex={shown ? 0 : -1}
        >
            {selectMode ? <Check size={18} aria-hidden="true" /> : <ListChecks size={18} aria-hidden="true" />}
            <span>{selectMode ? 'Done' : 'Select'}</span>
        </button>
    );
};
