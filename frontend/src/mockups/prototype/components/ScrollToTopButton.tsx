import React, { useEffect, useRef, useState } from 'react';
import { ArrowUp } from 'lucide-react';

// How far down .pt-body must be scrolled before the button is eligible to
// show at all.
const SHOW_AFTER_PX = 400;
// How long scrolling must have stopped before the button actually appears.
const SETTLE_MS = 150;

/**
 * Floating "scroll to top" button for .pt-body -- the app's one real scroll
 * container (see prototype.css's comment on .mock-body: the header/tabbar
 * are fixed siblings and only this scrolls; the actual document never does,
 * which is also why iOS Safari's tap-status-bar-to-scroll-to-top gesture
 * doesn't do anything here -- that gesture only resets the document's own
 * scroll position).
 *
 * Appears once scrolled past SHOW_AFTER_PX AND scrolling has settled for a
 * moment -- hidden while actively scrolling so it never sits over content
 * mid-gesture. Same shape as a chat app's "jump to the latest message"
 * button, just pointed the other way.
 */
export const ScrollToTopButton: React.FC = () => {
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
            if (container.scrollTop <= SHOW_AFTER_PX) {
                setVisible(false);
                return;
            }
            // Hidden while a scroll is actively in flight; reappears once it settles.
            setVisible(false);
            settleTimer.current = setTimeout(() => setVisible(true), SETTLE_MS);
        };
        container.addEventListener('scroll', onScroll, { passive: true });
        return () => {
            container.removeEventListener('scroll', onScroll);
            if (settleTimer.current) {
                clearTimeout(settleTimer.current);
            }
        };
    }, []);

    const scrollToTop = () => {
        document.querySelector<HTMLElement>('.pt-body')?.scrollTo({ top: 0, behavior: 'smooth' });
    };

    return (
        <button
            type="button"
            className={`pt-scroll-top${visible ? ' visible' : ''}`}
            onClick={scrollToTop}
            aria-label="Scroll to top"
            tabIndex={visible ? 0 : -1}
        >
            <ArrowUp size={20} aria-hidden="true" />
        </button>
    );
};
