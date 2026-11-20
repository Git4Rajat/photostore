import React from 'react';
import { ArrowUp } from 'lucide-react';
import { useFloatingClearance } from './useFloatingClearance';
import { useScrollSettle } from './useScrollSettle';

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
    const visible = useScrollSettle();
    // Lifts this button (and the Select pill) above any dock / bar / toast at the bottom.
    useFloatingClearance();

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
