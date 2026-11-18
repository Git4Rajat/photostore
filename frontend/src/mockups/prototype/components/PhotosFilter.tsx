// The Photos page's one Filter button and the panel behind it, as the redesign draws it: a sheet on
// phones, a popover under the button on wide screens. See docs/design-mockups (One Filter button).
import React, { useEffect, useRef, useState } from 'react';
import { Heart as HeartIcon, Star as StarIcon } from 'lucide-react';
import { BottomSheet } from './BottomSheet';
import type { CaptureRange, MediaFilter } from '../store';

export interface PhotosFilterState {
    mediaFilter: MediaFilter;
    likedOnly: boolean;
    /** Minimum stars, 0 for any. */
    rating: number;
    captureRange: CaptureRange | null;
}

export interface PhotosFilterActions {
    setMediaFilter: (value: MediaFilter) => void;
    setLikedOnly: (value: boolean) => void;
    setRating: (value: number) => void;
    setCaptureRange: (range: CaptureRange | null) => void;
}

// The redesign's own sliders glyph (two short rules, a knob on each), not the stock three-rule one.
const FilterIcon: React.FC<React.SVGProps<SVGSVGElement>> = (props) => (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" {...props}>
        <path d="M4 7.5h9M17 7.5h3M4 16.5h3M11 16.5h9" />
        <circle cx="15" cy="7.5" r="2" />
        <circle cx="9" cy="16.5" r="2" />
    </svg>
);

const MEDIA: { value: MediaFilter; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'photo', label: 'Photos' },
    { value: 'video', label: 'Videos' },
    { value: 'live', label: 'Live' },
];
const RATINGS = [0, 3, 4, 5];
const LAST_30_DAYS = 'Last 30 days';
const THIS_YEAR = 'This year';

const iso = (d: Date): string => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const day = (value: string): string => new Date(`${value}T12:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });

export const customRangeLabel = (start: string, end: string): string => (
    start && end ? `${day(start)} – ${day(end)}` : start ? `From ${day(start)}` : end ? `Until ${day(end)}` : 'Choose dates'
);

export const activeFilterCount = (s: PhotosFilterState): number => (
    (s.mediaFilter !== 'all' ? 1 : 0) + (s.likedOnly ? 1 : 0) + (s.rating > 0 ? 1 : 0) + (s.captureRange ? 1 : 0)
);

const mediaNoun = (m: MediaFilter): string => (m === 'video' ? 'Videos' : m === 'live' ? 'Live Photos' : m === 'photo' ? 'Photos' : 'Photos');

/** One plain sentence for what is on, e.g. "Videos you liked". */
export const FilterSentence: React.FC<{ state: PhotosFilterState }> = ({ state }) => {
    const tail = [
        state.likedOnly ? 'you liked' : '',
        state.rating > 0 ? `rated ${state.rating}${state.rating === 5 ? '' : '+'} stars` : '',
        state.captureRange ? `· ${state.captureRange.label}` : '',
    ].filter(Boolean).join(' ');
    return <span><b>{mediaNoun(state.mediaFilter)}</b>{tail ? ` ${tail}` : ''}</span>;
};

const useWide = (): boolean => {
    const query = '(min-width: 1022px)';
    const [wide, setWide] = useState(() => typeof window !== 'undefined' && Boolean(window.matchMedia?.(query).matches));
    useEffect(() => {
        const list = window.matchMedia?.(query);
        if (!list) return undefined;
        const on = () => setWide(list.matches);
        list.addEventListener('change', on);
        return () => list.removeEventListener('change', on);
    }, []);
    return wide;
};

const sameRange = (a: CaptureRange | null, b: CaptureRange | null): boolean => (
    (a?.start ?? '') === (b?.start ?? '') && (a?.end ?? '') === (b?.end ?? '') && (a?.label ?? '') === (b?.label ?? '')
);
const sameFilters = (a: PhotosFilterState, b: PhotosFilterState): boolean => (
    a.mediaFilter === b.mediaFilter && a.likedOnly === b.likedOnly && a.rating === b.rating && sameRange(a.captureRange, b.captureRange)
);

// On a phone the choices are held here as a draft and only applied when Show is tapped, so tapping
// around never reshuffles the photos behind the sheet. As a desktop popover (`live`) each choice
// applies at once, as the redesign draws it, so the photos behind update while it stays open.
const Panel: React.FC<{
    state: PhotosFilterState;
    actions: PhotosFilterActions;
    matchCount: number | null;
    live: boolean;
    onDone: () => void;
}> = ({ state, actions, matchCount, live, onDone }) => {
    const [draft, setDraft] = useState<PhotosFilterState>(state);
    const count = activeFilterCount(draft);
    const dirty = !live && !sameFilters(draft, state);
    const [choosing, setChoosing] = useState(Boolean(state.captureRange && ![LAST_30_DAYS, THIS_YEAR].includes(state.captureRange.label)));
    const [from, setFrom] = useState(state.captureRange?.start ?? '');
    const [to, setTo] = useState(state.captureRange?.end ?? '');
    const label = draft.captureRange?.label;
    const anyTime = !draft.captureRange && !choosing;

    const applyTo = (target: PhotosFilterState) => {
        if (target.mediaFilter !== state.mediaFilter) actions.setMediaFilter(target.mediaFilter);
        if (target.likedOnly !== state.likedOnly) actions.setLikedOnly(target.likedOnly);
        if (target.rating !== state.rating) actions.setRating(target.rating);
        if (!sameRange(target.captureRange, state.captureRange)) actions.setCaptureRange(target.captureRange);
    };
    const patch = (next: Partial<PhotosFilterState>) => {
        const target = { ...draft, ...next };
        setDraft(target);
        if (live) applyTo(target);
    };
    const pickRange = (next: CaptureRange | null) => { setChoosing(false); patch({ captureRange: next }); };
    const last30 = () => { const start = new Date(); start.setDate(start.getDate() - 30); pickRange({ start: iso(start), label: LAST_30_DAYS }); };
    const thisYear = () => pickRange({ start: `${new Date().getFullYear()}-01-01`, label: THIS_YEAR });
    const custom = (start: string, end: string) => {
        setFrom(start);
        setTo(end);
        patch({ captureRange: start || end ? { ...(start ? { start } : {}), ...(end ? { end } : {}), label: customRangeLabel(start, end) } : null });
    };
    const clearAll = () => {
        const cleared: PhotosFilterState = { mediaFilter: 'all', likedOnly: false, rating: 0, captureRange: null };
        setDraft(cleared);
        setChoosing(false); setFrom(''); setTo('');
        if (live) applyTo(cleared);
    };
    const apply = () => { applyTo(draft); onDone(); };

    return (
        <div className="pt-fp">
            <div className="pt-fp-head">
                <h4>Filter</h4>
                <button type="button" className="pt-linkish muted" disabled={count === 0} onClick={clearAll}>Clear all</button>
            </div>
            <div className="pt-fp-sec">
                <span className="pt-fp-label">Show</span>
                <div className="mock-seg" role="group" aria-label="Show">
                    {MEDIA.map((m) => (
                        <button key={m.value} type="button" className={draft.mediaFilter === m.value ? 'active' : undefined} aria-pressed={draft.mediaFilter === m.value} onClick={() => patch({ mediaFilter: m.value })}>
                            {m.label}
                        </button>
                    ))}
                </div>
            </div>
            <div className="pt-fp-row">
                <span><HeartIcon aria-hidden="true" />Liked only</span>
                <button type="button" role="switch" aria-checked={draft.likedOnly} aria-label="Liked only" className={`pt-switch${draft.likedOnly ? ' on' : ''}`} onClick={() => patch({ likedOnly: !draft.likedOnly })}><span aria-hidden="true" /></button>
            </div>
            <div className="pt-fp-sec">
                <span className="pt-fp-label">Date</span>
                <div className="pt-fp-chips">
                    <button type="button" className="chip" aria-pressed={anyTime} onClick={() => pickRange(null)}>Any time</button>
                    <button type="button" className="chip" aria-pressed={label === LAST_30_DAYS} onClick={last30}>{LAST_30_DAYS}</button>
                    <button type="button" className="chip" aria-pressed={label === THIS_YEAR} onClick={thisYear}>{THIS_YEAR}</button>
                    <button type="button" className="chip" aria-pressed={choosing} onClick={() => { setChoosing(true); custom(from, to); }}>Choose dates</button>
                </div>
                {choosing && (
                    <div className="pt-fp-dates">
                        <label>From<input type="date" aria-label="From date" value={from} max={to || undefined} onChange={(e) => custom(e.target.value, to)} /></label>
                        <label>To<input type="date" aria-label="To date" value={to} min={from || undefined} onChange={(e) => custom(from, e.target.value)} /></label>
                    </div>
                )}
            </div>
            <div className="pt-fp-sec">
                <span className="pt-fp-label">Rating</span>
                <div className="pt-fp-chips">
                    {RATINGS.map((r) => (
                        <button key={r} type="button" className="chip" aria-pressed={draft.rating === r} onClick={() => patch({ rating: r })}>
                            {r === 0 ? 'Any' : <><StarIcon aria-hidden="true" />{r === 5 ? '5' : `${r}+`}</>}
                        </button>
                    ))}
                </div>
            </div>
            <div className="pt-fp-foot">
                {!dirty && matchCount !== null && <span className="pt-fp-count">{matchCount.toLocaleString()} item{matchCount === 1 ? '' : 's'}</span>}
                <button type="button" className="btn btn-primary" onClick={apply}>
                    {dirty ? 'Show results' : matchCount === null ? 'Done' : `Show ${matchCount.toLocaleString()} item${matchCount === 1 ? '' : 's'}`}
                </button>
            </div>
        </div>
    );
};

/** The Filter button with the item count beside it, plus the panel it opens. */
export const PhotosFilterBar: React.FC<{
    state: PhotosFilterState;
    actions: PhotosFilterActions;
    /** Photos matching the filters now, when known. */
    matchCount: number | null;
    onSelect: () => void;
}> = ({ state, actions, matchCount, onSelect }) => {
    const [open, setOpen] = useState(false);
    const wide = useWide();
    const wrapRef = useRef<HTMLDivElement | null>(null);
    const count = activeFilterCount(state);

    useEffect(() => {
        if (!open || !wide) return undefined;
        const onDown = (e: MouseEvent) => { if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false); };
        const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
        document.addEventListener('mousedown', onDown);
        document.addEventListener('keydown', onKey);
        return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey); };
    }, [open, wide]);

    const panel = <Panel key={open ? 'open' : 'closed'} state={state} actions={actions} matchCount={matchCount} live={wide} onDone={() => setOpen(false)} />;
    return (
        <div className="pt-chips pt-fbar" role="group" aria-label="Filter photos" ref={wrapRef}>
            <button
                type="button"
                className={`btn btn-sm pt-fbtn${count > 0 ? ' on' : ''}`}
                aria-haspopup="dialog"
                aria-expanded={open}
                aria-label={count > 0 ? `Filter, ${count} on` : 'Filter'}
                onClick={() => setOpen(!open)}
            >
                <FilterIcon aria-hidden="true" />Filter
                {count > 0 && <span className="pt-fcnt" aria-hidden="true">{count}</span>}
            </button>
            {matchCount !== null && <span className="pt-fmeta">{matchCount.toLocaleString()} item{matchCount === 1 ? '' : 's'}</span>}
            <button type="button" className="pt-linkish pt-chip-select" onClick={onSelect}>Select</button>
            {open && wide && <div className="pt-filter-pop" role="dialog" aria-label="Filter photos">{panel}</div>}
            {!wide && <BottomSheet open={open} onClose={() => setOpen(false)} title="Filter photos">{panel}</BottomSheet>}
        </div>
    );
};
