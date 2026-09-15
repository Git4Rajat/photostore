import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import type { TimelineSummary } from '../../types/timeline';

type ZoomLevel = 'year' | 'month' | 'day';

interface TimelineViewState {
    startDay: number;
    endDay: number;
}

interface FlatBucket {
    startDay: number;
    endDay: number; // exclusive
    count: number;
    label: string;
    key: string;
}

interface TimelineProps {
    summary: TimelineSummary;
    currentStartISO: string;
    currentEndISO: string;
    onRangeSettled: (startISO: string, endISO: string) => void;
    className?: string;
}

const MS_PER_DAY = 86400000;
const MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// Rendering-level thresholds, applied to the visible span (not tracked as
// separate state) so level and range can never drift out of sync.
const YEAR_LEVEL_MIN_SPAN_DAYS = 2 * 365;
const MONTH_LEVEL_MIN_SPAN_DAYS = 45;
const MIN_VISIBLE_SPAN_DAYS = 3;

// "Ignore older years" heuristic: hide the leading run of years (from the
// very start of the library) with fewer than this many photos -- UI-only
// tuning, never affects what's searchable.
const SPARSE_YEAR_THRESHOLD = 50;

// Timeline's own "user has settled" debounce before pushing a range up to the
// parent. Independent from, and composes with, PhotoGallery's existing
// 350ms debounced fetch effect -- this one only decides *when* to call
// onRangeSettled, not when to hit the network.
const SETTLE_DEBOUNCE_MS = 300;
const CLICK_MOVEMENT_THRESHOLD_PX = 6;
const AXIS_TARGET_TICKS = 8;

const clamp = (value: number, min: number, max: number): number => Math.min(Math.max(value, min), max);

const isoToUtcMs = (iso: string): number => {
    const [y, m, d] = iso.split('-').map(Number);
    return Date.UTC(y, (m || 1) - 1, d || 1);
};
const utcMsToIso = (ms: number): string => new Date(ms).toISOString().slice(0, 10);
const dayIndexFromISO = (iso: string, originMs: number): number => Math.round((isoToUtcMs(iso) - originMs) / MS_PER_DAY);

const daysInMonthUTC = (year: number, month1to12: number): number => {
    const nextMonth = month1to12 === 12 ? 1 : month1to12 + 1;
    const nextYear = month1to12 === 12 ? year + 1 : year;
    return Math.round((Date.UTC(nextYear, nextMonth - 1, 1) - Date.UTC(year, month1to12 - 1, 1)) / MS_PER_DAY);
};
const daysInYearUTC = (year: number): number => Math.round((Date.UTC(year + 1, 0, 1) - Date.UTC(year, 0, 1)) / MS_PER_DAY);

const buildYearBuckets = (summary: TimelineSummary, originMs: number): FlatBucket[] => (
    Object.entries(summary.years)
        .map(([yearStr, bucket]) => {
            const year = Number(yearStr);
            const startDay = Math.round((Date.UTC(year, 0, 1) - originMs) / MS_PER_DAY);
            return {
                startDay,
                endDay: startDay + daysInYearUTC(year),
                count: bucket.count,
                label: yearStr,
                key: `y-${yearStr}`,
            };
        })
        .sort((a, b) => a.startDay - b.startDay)
);

const buildMonthBuckets = (summary: TimelineSummary, originMs: number): FlatBucket[] => {
    const out: FlatBucket[] = [];
    Object.entries(summary.years).forEach(([yearStr, yearBucket]) => {
        const year = Number(yearStr);
        Object.entries(yearBucket.months).forEach(([monthStr, monthBucket]) => {
            const month = Number(monthStr);
            const startDay = Math.round((Date.UTC(year, month - 1, 1) - originMs) / MS_PER_DAY);
            out.push({
                startDay,
                endDay: startDay + daysInMonthUTC(year, month),
                count: monthBucket.count,
                label: `${MONTH_SHORT[month - 1]} ${yearStr}`,
                key: `m-${yearStr}-${monthStr}`,
            });
        });
    });
    return out.sort((a, b) => a.startDay - b.startDay);
};

const buildDayBuckets = (summary: TimelineSummary, originMs: number): FlatBucket[] => {
    const out: FlatBucket[] = [];
    Object.entries(summary.years).forEach(([yearStr, yearBucket]) => {
        const year = Number(yearStr);
        Object.entries(yearBucket.months).forEach(([monthStr, monthBucket]) => {
            const month = Number(monthStr);
            Object.entries(monthBucket.days).forEach(([dayStr, count]) => {
                const day = Number(dayStr);
                const startDay = Math.round((Date.UTC(year, month - 1, day) - originMs) / MS_PER_DAY);
                out.push({
                    startDay,
                    endDay: startDay + 1,
                    count,
                    label: `${MONTH_SHORT[month - 1]} ${day}, ${yearStr}`,
                    key: `d-${yearStr}-${monthStr}-${dayStr}`,
                });
            });
        });
    });
    return out.sort((a, b) => a.startDay - b.startDay);
};

// Drops a leading run of sparse, gapped-off old years from what's *drawn* at
// the year level. Walking newest-to-oldest, the first adjacent pair where the
// older year is both sparse and separated by a real gap marks the cut --
// everything older than that is hidden (but still reachable via the manual
// date inputs, and always still searchable). A mid-timeline dip below the
// threshold (e.g. one quiet year between two active ones) is left alone --
// only the leading run right at the start of the library is ever hidden.
const computeVisibleYears = (
    yearCounts: { year: number; count: number }[],
    threshold = SPARSE_YEAR_THRESHOLD,
): Set<number> => {
    const sorted = [...yearCounts].sort((a, b) => a.year - b.year);
    if (sorted.length === 0) {
        return new Set();
    }
    let cutoffIndex = 0;
    // Never hide every year -- stop one short of the end so the most recent
    // year always stays visible, even for a brand-new, still-sparse library.
    while (cutoffIndex < sorted.length - 1 && sorted[cutoffIndex].count < threshold) {
        cutoffIndex += 1;
    }
    return new Set(sorted.slice(cutoffIndex).map((entry) => entry.year));
};

const levelForSpan = (spanDays: number): ZoomLevel => {
    if (spanDays > YEAR_LEVEL_MIN_SPAN_DAYS) {
        return 'year';
    }
    if (spanDays > MONTH_LEVEL_MIN_SPAN_DAYS) {
        return 'month';
    }
    return 'day';
};

// Standard "zoom to cursor" formula: find the day under the pointer, scale
// the visible span by `factor` (>1 zooms in, <1 zooms out), then re-center so
// that day stays under the pointer, clamped to the library's full domain.
const zoomAroundPoint = (
    view: TimelineViewState,
    pointerFraction: number,
    factor: number,
    totalSpanDays: number,
): TimelineViewState => {
    const span = view.endDay - view.startDay;
    const pointerDay = view.startDay + pointerFraction * span;
    const newSpan = clamp(span / factor, MIN_VISIBLE_SPAN_DAYS, totalSpanDays);
    let newStart = pointerDay - pointerFraction * newSpan;
    let newEnd = newStart + newSpan;
    if (newStart < 0) {
        newEnd -= newStart;
        newStart = 0;
    }
    if (newEnd > totalSpanDays) {
        newStart -= newEnd - totalSpanDays;
        newEnd = totalSpanDays;
    }
    return { startDay: Math.max(0, newStart), endDay: Math.min(totalSpanDays, newEnd) };
};

const panView = (view: TimelineViewState, deltaDays: number, totalSpanDays: number): TimelineViewState => {
    const span = view.endDay - view.startDay;
    let newStart = view.startDay + deltaDays;
    let newEnd = view.endDay + deltaDays;
    if (newStart < 0) {
        newEnd -= newStart;
        newStart = 0;
    }
    if (newEnd > totalSpanDays) {
        newStart -= newEnd - totalSpanDays;
        newEnd = totalSpanDays;
    }
    return { startDay: clamp(newStart, 0, totalSpanDays), endDay: clamp(newEnd, 0, totalSpanDays) };
};

// A click/scrub selects the whole bucket at the current zoom level -- a full
// year, a full month, or a single day -- and that bucket becomes the range
// pushed to the gallery's capture-date filter.
const resolveSelectionRange = (day: number, level: ZoomLevel, originMs: number): [string, string] => {
    const ms = originMs + Math.round(day) * MS_PER_DAY;
    const dt = new Date(ms);
    const year = dt.getUTCFullYear();
    const month = dt.getUTCMonth(); // 0-based
    if (level === 'year') {
        return [utcMsToIso(Date.UTC(year, 0, 1)), utcMsToIso(Date.UTC(year, 11, 31))];
    }
    if (level === 'month') {
        const lastDay = daysInMonthUTC(year, month + 1);
        return [utcMsToIso(Date.UTC(year, month, 1)), utcMsToIso(Date.UTC(year, month, lastDay))];
    }
    const iso = utcMsToIso(ms);
    return [iso, iso];
};

interface AxisTick {
    key: string;
    label: string;
    leftPct: number;
}

const thinTicks = (ticks: AxisTick[], target: number): AxisTick[] => {
    if (ticks.length <= target) {
        return ticks;
    }
    const step = Math.ceil(ticks.length / target);
    return ticks.filter((_, index) => index % step === 0);
};

// Calendar-grid axis labels, independent of which buckets actually have
// photos -- so a quiet stretch of the timeline still shows "where" it is.
const buildAxisLabels = (level: ZoomLevel, view: TimelineViewState, originMs: number): AxisTick[] => {
    const spanDays = view.endDay - view.startDay;
    if (spanDays <= 0) {
        return [];
    }
    const startMs = originMs + view.startDay * MS_PER_DAY;
    const endMs = originMs + view.endDay * MS_PER_DAY;
    const leftPctFor = (day: number) => ((day - view.startDay) / spanDays) * 100;

    if (level === 'year') {
        const startYear = new Date(startMs).getUTCFullYear();
        const endYear = new Date(endMs).getUTCFullYear();
        const step = Math.max(1, Math.ceil((endYear - startYear + 1) / AXIS_TARGET_TICKS));
        const ticks: AxisTick[] = [];
        for (let year = startYear; year <= endYear; year += step) {
            const day = Math.round((Date.UTC(year, 0, 1) - originMs) / MS_PER_DAY);
            ticks.push({ key: `y-${year}`, label: String(year), leftPct: leftPctFor(day) });
        }
        return ticks;
    }

    if (level === 'month') {
        let year = new Date(startMs).getUTCFullYear();
        let month = new Date(startMs).getUTCMonth();
        const ticks: AxisTick[] = [];
        while (Date.UTC(year, month, 1) <= endMs) {
            const day = Math.round((Date.UTC(year, month, 1) - originMs) / MS_PER_DAY);
            ticks.push({ key: `m-${year}-${month}`, label: MONTH_SHORT[month], leftPct: leftPctFor(day) });
            month += 1;
            if (month > 11) {
                month = 0;
                year += 1;
            }
        }
        return thinTicks(ticks, AXIS_TARGET_TICKS);
    }

    const dayMarks = [1, 5, 10, 15, 20, 25];
    let year = new Date(startMs).getUTCFullYear();
    let month = new Date(startMs).getUTCMonth();
    const ticks: AxisTick[] = [];
    while (Date.UTC(year, month, 1) <= endMs) {
        const lastDay = daysInMonthUTC(year, month + 1);
        const marks = Array.from(new Set([...dayMarks.filter((d) => d <= lastDay), lastDay]));
        marks.forEach((mark) => {
            const day = Math.round((Date.UTC(year, month, mark) - originMs) / MS_PER_DAY);
            if (day >= view.startDay && day <= view.endDay) {
                ticks.push({ key: `d-${year}-${month}-${mark}`, label: String(mark), leftPct: leftPctFor(day) });
            }
        });
        month += 1;
        if (month > 11) {
            month = 0;
            year += 1;
        }
    }
    return ticks;
};

// Zoomable year -> month -> day photo-count timeline. Renders entirely from
// the already-fetched `summary` (see TimelineMetadataProvider) -- zoom, pan,
// hover and click never touch the network. Only a settled selection (via
// onRangeSettled) does, by driving PhotoGallery's existing captureStartDate/
// captureEndDate state.
const Timeline: React.FC<TimelineProps> = ({ summary, currentStartISO, currentEndISO, onRangeSettled, className }) => {
    const trackRef = useRef<HTMLDivElement | null>(null);
    const hasData = summary.firstDate !== null && summary.lastDate !== null;

    const visibleYears = useMemo(() => computeVisibleYears(
        Object.entries(summary.years).map(([year, bucket]) => ({ year: Number(year), count: bucket.count })),
    ), [summary]);

    // The navigable domain starts at the first *visible* year, not the raw
    // firstDate -- an ignored leading run of sparse years gets no timeline
    // real estate at all (not even a blank stretch with tick labels), so
    // there's no way to scrub/zoom into it. Those years are still reachable
    // via the manual date inputs and search, just not via the timeline.
    const originMs = useMemo(() => {
        if (visibleYears.size > 0) {
            return Date.UTC(Math.min(...Array.from(visibleYears)), 0, 1);
        }
        return summary.firstDate ? isoToUtcMs(summary.firstDate) : 0;
    }, [visibleYears, summary.firstDate]);
    const totalSpanDays = useMemo(() => (
        hasData ? Math.max(MIN_VISIBLE_SPAN_DAYS, dayIndexFromISO(summary.lastDate as string, originMs) + 1) : 0
    ), [hasData, summary.lastDate, originMs]);

    const yearBuckets = useMemo(() => buildYearBuckets(summary, originMs), [summary, originMs]);
    const monthBuckets = useMemo(() => buildMonthBuckets(summary, originMs), [summary, originMs]);
    const dayBuckets = useMemo(() => buildDayBuckets(summary, originMs), [summary, originMs]);

    const [view, setView] = useState<TimelineViewState>({ startDay: 0, endDay: 0 });
    const viewInitializedRef = useRef(false);
    useEffect(() => {
        if (hasData && !viewInitializedRef.current) {
            setView({ startDay: 0, endDay: totalSpanDays });
            viewInitializedRef.current = true;
        }
    }, [hasData, totalSpanDays]);

    const [selectedDay, setSelectedDay] = useState<number | null>(null);
    const [hoverDay, setHoverDay] = useState<number | null>(null);
    const lastEmittedRef = useRef<{ start: string; end: string } | null>(null);

    // Stay in sync with the controlled props: a manual edit in the date-input
    // filter (or a Reset) should reposition the marker too. Skip re-syncing
    // whatever Timeline itself just emitted, to avoid a feedback loop.
    useEffect(() => {
        if (!hasData) {
            return;
        }
        const incoming = currentStartISO && currentEndISO ? { start: currentStartISO, end: currentEndISO } : null;
        const lastEmitted = lastEmittedRef.current;
        if (incoming && lastEmitted && incoming.start === lastEmitted.start && incoming.end === lastEmitted.end) {
            return;
        }
        if (!incoming) {
            setSelectedDay(null);
            lastEmittedRef.current = null;
            return;
        }
        const startIdx = dayIndexFromISO(incoming.start, originMs);
        const endIdx = dayIndexFromISO(incoming.end, originMs);
        setSelectedDay(Math.round((startIdx + endIdx) / 2));
        lastEmittedRef.current = incoming;
    }, [currentStartISO, currentEndISO, hasData, originMs]);

    const level = levelForSpan(view.endDay - view.startDay);
    const buckets = level === 'year' ? yearBuckets : level === 'month' ? monthBuckets : dayBuckets;

    const visibleBuckets = useMemo(() => buckets.filter((bucket) => (
        bucket.endDay > view.startDay
        && bucket.startDay < view.endDay
        && (level !== 'year' || visibleYears.has(Number(bucket.label)))
    )), [buckets, view.startDay, view.endDay, level, visibleYears]);

    const maxCount = useMemo(() => visibleBuckets.reduce((max, bucket) => Math.max(max, bucket.count), 1), [visibleBuckets]);
    const axisTicks = useMemo(() => buildAxisLabels(level, view, originMs), [level, view, originMs]);

    const settleTimerRef = useRef<number | null>(null);
    const commitSelection = useCallback((day: number, immediate: boolean) => {
        if (settleTimerRef.current !== null) {
            window.clearTimeout(settleTimerRef.current);
            settleTimerRef.current = null;
        }
        const fire = () => {
            const [startISO, endISO] = resolveSelectionRange(day, level, originMs);
            lastEmittedRef.current = { start: startISO, end: endISO };
            onRangeSettled(startISO, endISO);
        };
        if (immediate) {
            fire();
        } else {
            settleTimerRef.current = window.setTimeout(fire, SETTLE_DEBOUNCE_MS);
        }
    }, [level, originMs, onRangeSettled]);

    useEffect(() => () => {
        if (settleTimerRef.current !== null) {
            window.clearTimeout(settleTimerRef.current);
        }
    }, []);

    // Zoom/pan (view) and bucket selection are desktop-mouse-friendly (wheel,
    // right-click, double-click) but touch has only pinch and tap -- tapping
    // narrows the filter further rather than clearing it, so touch users who
    // drill into a day/month have no gesture to get back to the full library.
    // This button is the explicit escape hatch for that, on every input type.
    const handleClearSelection = useCallback((e: React.MouseEvent<HTMLButtonElement>) => {
        e.stopPropagation();
        if (settleTimerRef.current !== null) {
            window.clearTimeout(settleTimerRef.current);
            settleTimerRef.current = null;
        }
        setSelectedDay(null);
        setView({ startDay: 0, endDay: totalSpanDays });
        lastEmittedRef.current = null;
        onRangeSettled('', '');
    }, [totalSpanDays, onRangeSettled]);

    const dragStateRef = useRef<{ pointerId: number; startX: number; startY: number; moved: boolean } | null>(null);
    const pinchStateRef = useRef<Map<number, { x: number; y: number }>>(new Map());

    const fractionFromClientX = useCallback((clientX: number): number => {
        const el = trackRef.current;
        if (!el) {
            return 0.5;
        }
        const rect = el.getBoundingClientRect();
        return rect.width > 0 ? clamp((clientX - rect.left) / rect.width, 0, 1) : 0.5;
    }, []);

    const dayFromClientX = useCallback((clientX: number): number => (
        view.startDay + fractionFromClientX(clientX) * (view.endDay - view.startDay)
    ), [view.startDay, view.endDay, fractionFromClientX]);

    const handlePointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        if (!hasData) {
            return;
        }
        e.currentTarget.setPointerCapture(e.pointerId);
        pinchStateRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
        dragStateRef.current = pinchStateRef.current.size === 1
            ? { pointerId: e.pointerId, startX: e.clientX, startY: e.clientY, moved: false }
            : null; // a second pointer landing turns this into a pinch, not a drag
    }, [hasData]);

    const handlePointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        if (!hasData) {
            return;
        }
        const day = dayFromClientX(e.clientX);
        setHoverDay(day);

        if (pinchStateRef.current.size === 2 && pinchStateRef.current.has(e.pointerId)) {
            const prevPts = Array.from(pinchStateRef.current.values());
            pinchStateRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
            const pts = Array.from(pinchStateRef.current.values());
            const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
            const prevDist = Math.hypot(prevPts[0].x - prevPts[1].x, prevPts[0].y - prevPts[1].y);
            if (prevDist > 0 && dist > 0) {
                const factor = dist / prevDist;
                const midX = (pts[0].x + pts[1].x) / 2;
                setView((v) => zoomAroundPoint(v, fractionFromClientX(midX), factor, totalSpanDays));
            }
            return;
        }

        const drag = dragStateRef.current;
        if (!drag || drag.pointerId !== e.pointerId) {
            return;
        }
        if (!drag.moved && Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) > CLICK_MOVEMENT_THRESHOLD_PX) {
            drag.moved = true;
        }
        if (drag.moved) {
            setSelectedDay(day);
            commitSelection(day, false);
        }
    }, [hasData, dayFromClientX, fractionFromClientX, totalSpanDays, commitSelection]);

    const handlePointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        pinchStateRef.current.delete(e.pointerId);
        const drag = dragStateRef.current;
        dragStateRef.current = null;
        if (!hasData || !drag || drag.pointerId !== e.pointerId) {
            return;
        }
        const day = dayFromClientX(e.clientX);
        setSelectedDay(day);
        // A tap/click (never crossed the movement threshold) commits right
        // away; a drag-release still goes through the settle debounce so a
        // fast flick across the timeline doesn't fire a request for every
        // intermediate position it passed through.
        commitSelection(day, !drag.moved);
    }, [hasData, dayFromClientX, commitSelection]);

    const handlePointerLeave = useCallback(() => {
        setHoverDay(null);
    }, []);

    // Wheel needs a non-passive native listener so preventDefault() reliably
    // stops page scroll -- React's onWheel prop attaches a passive listener.
    useEffect(() => {
        const el = trackRef.current;
        if (!el || !hasData) {
            return undefined;
        }
        const handleWheel = (e: WheelEvent) => {
            e.preventDefault();
            const rect = el.getBoundingClientRect();
            const fraction = rect.width > 0 ? clamp((e.clientX - rect.left) / rect.width, 0, 1) : 0.5;
            if (e.shiftKey) {
                const deltaDays = ((e.deltaX || e.deltaY) / Math.max(1, rect.width)) * (view.endDay - view.startDay);
                setView((v) => panView(v, deltaDays, totalSpanDays));
            } else {
                const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
                setView((v) => zoomAroundPoint(v, fraction, factor, totalSpanDays));
            }
        };
        el.addEventListener('wheel', handleWheel, { passive: false });
        return () => el.removeEventListener('wheel', handleWheel);
    }, [hasData, totalSpanDays, view.startDay, view.endDay]);

    const handleDoubleClick = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
        if (!hasData) {
            return;
        }
        setView((v) => zoomAroundPoint(v, fractionFromClientX(e.clientX), 2, totalSpanDays));
    }, [hasData, fractionFromClientX, totalSpanDays]);

    const handleContextMenu = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
        e.preventDefault();
        if (!hasData) {
            return;
        }
        setView((v) => zoomAroundPoint(v, fractionFromClientX(e.clientX), 0.5, totalSpanDays));
    }, [hasData, fractionFromClientX, totalSpanDays]);

    if (!hasData) {
        return (
            <div className={`timeline-widget timeline-widget-empty ${className || ''}`}>
                <p className="timeline-empty-message">No dated photos yet.</p>
            </div>
        );
    }

    const spanDays = Math.max(1, view.endDay - view.startDay);
    const tooltipDay = hoverDay ?? selectedDay;
    const tooltipBucket = tooltipDay !== null
        ? buckets.find((bucket) => tooltipDay >= bucket.startDay && tooltipDay < bucket.endDay)
        : null;
    const tooltipLeftPct = tooltipDay !== null
        ? clamp(((tooltipDay - view.startDay) / spanDays) * 100, 4, 96)
        : 50;

    const hasActiveSelection = Boolean(currentStartISO && currentEndISO);

    return (
        <div className={`timeline-widget ${className || ''}`}>
            {hasActiveSelection && (
                <button
                    type="button"
                    className="timeline-clear-btn"
                    onClick={handleClearSelection}
                    aria-label="Clear date selection and show entire library"
                    title="Show entire library"
                >
                    <XMarkIcon className="toolbar-icon" />
                    Show all
                </button>
            )}
            <div
                ref={trackRef}
                className="timeline-track"
                onPointerDown={handlePointerDown}
                onPointerMove={handlePointerMove}
                onPointerUp={handlePointerUp}
                onPointerCancel={handlePointerUp}
                onPointerLeave={handlePointerLeave}
                onDoubleClick={handleDoubleClick}
                onContextMenu={handleContextMenu}
                role="slider"
                aria-label="Photo timeline"
                aria-valuemin={0}
                aria-valuemax={totalSpanDays}
                aria-valuenow={selectedDay ?? 0}
            >
                <div className="timeline-bars">
                    {visibleBuckets.map((bucket) => {
                        const leftPct = ((bucket.startDay - view.startDay) / spanDays) * 100;
                        const widthPct = Math.max(0.4, ((bucket.endDay - bucket.startDay) / spanDays) * 100);
                        const heightPct = Math.max(4, (bucket.count / maxCount) * 100);
                        const isSelected = selectedDay !== null && selectedDay >= bucket.startDay && selectedDay < bucket.endDay;
                        return (
                            <div
                                key={bucket.key}
                                className={`timeline-bar${isSelected ? ' timeline-bar-selected' : ''}`}
                                style={{ left: `${leftPct}%`, width: `${widthPct}%`, height: `${heightPct}%` }}
                                title={`${bucket.label} · ${bucket.count}`}
                            />
                        );
                    })}
                </div>
                <div className="timeline-axis">
                    {axisTicks.map((tick) => (
                        <span key={tick.key} className="timeline-tick" style={{ left: `${tick.leftPct}%` }}>
                            {tick.label}
                        </span>
                    ))}
                </div>
            </div>
            {tooltipBucket && (
                <div className="timeline-tooltip" style={{ left: `${tooltipLeftPct}%` }}>
                    {tooltipBucket.label} · {tooltipBucket.count} photo{tooltipBucket.count === 1 ? '' : 's'}
                </div>
            )}
        </div>
    );
};

export default Timeline;
