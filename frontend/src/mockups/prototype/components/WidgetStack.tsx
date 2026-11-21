import React, { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevGlyph } from './RedesignIcons';
import { IconButton } from '../../../components/ui/Button';
import { plural } from '../../../utils/format';

// Live or just-finished activities (upload, photo processing, ...) share one stack at the bottom of the
// screen. Toasts are not part of it. Collapsed: the newest card in front with older ones peeking behind it
// and a count badge. Expanded: every card in full, newest on top, with a header.

/** Cards beyond this many are folded away behind the front ones. */
export const MAX_VISIBLE_LAYERS = 3;
export const STACK_HEIGHT_VAR = '--pt-stack-h';

interface Entry { at: number; finished: boolean; dismiss: () => void }

interface StackApi {
    host: HTMLElement | null;
    rankOf: (id: string) => number;
    open: boolean;
    register: (id: string, entry: Omit<Entry, 'at'>) => void;
    unregister: (id: string) => void;
}

const StackContext = createContext<StackApi | null>(null);

export const WidgetStackProvider: React.FC<{ children?: React.ReactNode }> = ({ children }) => {
    const [entries, setEntries] = useState<Record<string, Entry>>({});
    const [expanded, setExpanded] = useState(false);
    const [layers, setLayers] = useState<HTMLElement | null>(null);
    const hostRef = useRef<HTMLDivElement | null>(null);
    const counter = useRef(0);

    const register = useCallback((id: string, entry: Omit<Entry, 'at'>) => {
        setEntries((previous) => {
            const existing = previous[id];
            if (existing && existing.finished === entry.finished && existing.dismiss === entry.dismiss) return previous;
            return { ...previous, [id]: { ...entry, at: existing ? existing.at : ++counter.current } };
        });
    }, []);
    const unregister = useCallback((id: string) => {
        setEntries((previous) => {
            if (!(id in previous)) return previous;
            const next = { ...previous };
            delete next[id];
            return next;
        });
    }, []);

    const ids = useMemo(() => Object.keys(entries).sort((a, b) => entries[b].at - entries[a].at), [entries]);
    const count = ids.length;
    const stacked = count > 1;
    const open = expanded && stacked;
    const finishedCount = ids.filter((id) => entries[id].finished).length;

    // Going back to one card drops the stack; the next time several are there it starts folded.
    useEffect(() => { if (!stacked) setExpanded(false); }, [stacked]);

    // Toasts and the floating buttons read the stack's height from the page.
    useEffect(() => {
        const root = document.documentElement;
        const host = hostRef.current;
        if (!host || count === 0) { root.style.removeProperty(STACK_HEIGHT_VAR); return undefined; }
        const publish = () => root.style.setProperty(STACK_HEIGHT_VAR, `${Math.round(host.getBoundingClientRect().height)}px`);
        publish();
        const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(publish);
        observer?.observe(host);
        return () => { observer?.disconnect(); root.style.removeProperty(STACK_HEIGHT_VAR); };
    }, [count]);

    useEffect(() => {
        if (!open) return undefined;
        const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false); };
        document.addEventListener('keydown', onKey);
        return () => document.removeEventListener('keydown', onKey);
    }, [open]);

    const rankOf = useCallback((id: string) => Math.max(0, ids.indexOf(id)), [ids]);
    const api = useMemo<StackApi>(() => ({ host: layers, rankOf, open, register, unregister }), [layers, rankOf, open, register, unregister]);

    const clearAll = () => { ids.forEach((id) => { if (entries[id].finished) entries[id].dismiss(); }); };
    // A tap on a folded stack opens it instead of reaching the card's own buttons.
    // The cards are portalled in, so this listens on the element itself rather than through React.
    useEffect(() => {
        const host = hostRef.current;
        if (!host || open || !stacked) return undefined;
        const unfold = (event: Event) => {
            event.stopPropagation();
            event.preventDefault();
            setExpanded(true);
        };
        host.addEventListener('click', unfold, true);
        return () => host.removeEventListener('click', unfold, true);
    }, [open, stacked]);
    const peek = stacked && !open ? Math.min(count, MAX_VISIBLE_LAYERS) - 1 : 0;

    return (
        <StackContext.Provider value={api}>
            {children}
            {open ? <div className="wstk-scrim" onClick={() => setExpanded(false)} aria-hidden="true" /> : null}
            <div
                ref={hostRef}
                className={`wstk${stacked ? (open ? ' open' : ' col') : ''}`}
                hidden={count === 0}
                style={peek ? { paddingTop: (peek === 1 ? 18 : 36) + 14 } : undefined}
                role="group"
                aria-label={stacked ? `${plural(count, 'activity')}${open ? '' : ', tap to show all'}` : undefined}
            >
                {open ? (
                    <div className="wh">
                        <b>{count} activities</b>
                        <button type="button" className="up-link" onClick={clearAll} disabled={finishedCount === 0}>Clear all</button>
                        <IconButton label="Collapse" variant="link" size="sm" onClick={() => setExpanded(false)}>
                            <ChevGlyph style={{ transform: 'rotate(90deg)' }} aria-hidden="true" />
                        </IconButton>
                    </div>
                ) : null}
                <div className="wstk-layers" ref={setLayers} />
                {stacked && !open ? <span className="wcnt" aria-hidden="true">{count}</span> : null}
            </div>
        </StackContext.Provider>
    );
};

interface StackWidgetProps {
    /** Stable per activity. */
    id: string;
    /** Done and only waiting to be dismissed: "Clear all" removes it and nothing else. */
    finished?: boolean;
    /** Removes the card; called by "Clear all" when it is finished. */
    onDismiss?: () => void;
    children: React.ReactNode;
}

/** Puts a card into the shared stack. Without a stack around it (as in the unit tests) the card renders where it is. */
export const StackWidget: React.FC<StackWidgetProps> = ({ id, finished = false, onDismiss, children }) => {
    const stack = useContext(StackContext);
    const dismissRef = useRef(onDismiss);
    dismissRef.current = onDismiss;
    const dismiss = useCallback(() => dismissRef.current?.(), []);
    const register = stack?.register;
    const unregister = stack?.unregister;

    useLayoutEffect(() => { register?.(id, { finished, dismiss }); }, [register, id, finished, dismiss]);
    useLayoutEffect(() => () => unregister?.(id), [unregister, id]);

    if (!stack) return <>{children}</>;
    if (!stack.host) return null;
    const rank = stack.rankOf(id);
    const hidden = !stack.open && rank >= MAX_VISIBLE_LAYERS;
    const behind = !stack.open && rank > 0;
    const inert = behind ? { inert: '' } : {};
    return createPortal(
        <div
            className={`wc-slot${behind ? ` b${Math.min(rank, MAX_VISIBLE_LAYERS)}` : ''}${hidden ? ' gone' : ''}`}
            style={{ order: rank }}
            aria-hidden={behind || undefined}
            {...inert}
        >
            {children}
        </div>,
        stack.host,
    );
};
