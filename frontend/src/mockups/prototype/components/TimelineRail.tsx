import React, { useState } from 'react';
import type { CaptureRange, TimelineSummary } from '../store';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = (n: number | string) => String(n).padStart(2, '0');
const lastDayOf = (year: string, month: string) => new Date(Number(year), Number(month), 0).getDate();

const yearRange = (y: string): CaptureRange => ({ start: `${y}-01-01`, end: `${y}-12-31`, label: y });
const monthRange = (y: string, m: string): CaptureRange => ({
    start: `${y}-${m}-01`, end: `${y}-${m}-${pad(lastDayOf(y, m))}`, label: `${MONTHS[Number(m) - 1]} ${y}`,
});
const dayRange = (y: string, m: string, d: string): CaptureRange => ({
    start: `${y}-${m}-${d}`, end: `${y}-${m}-${d}`, label: `${MONTHS[Number(m) - 1]} ${Number(d)}, ${y}`,
});

interface Props {
    timeline: TimelineSummary;
    captureRange: CaptureRange | null;
    onSelect: (range: CaptureRange | null) => void;
}

/**
 * Zoomable date scrubber: All → year → month → day. Each level is a horizontal
 * chip strip; a breadcrumb above it walks back up. Counts come straight from the
 * /photos/timeline summary tree, so they always agree with the gallery.
 */
export const TimelineRail: React.FC<Props> = ({ timeline, captureRange, onSelect }) => {
    // Internal drill state; captureRange stays the source of truth for the query.
    const [year, setYear] = useState<string | null>(null);
    const [month, setMonth] = useState<string | null>(null);

    const years = Object.keys(timeline.years).sort().reverse();
    if (years.length <= 1 && timeline.undatedCount === 0) return null;

    const reset = () => { setYear(null); setMonth(null); onSelect(null); };
    const pickYear = (y: string) => { setYear(y); setMonth(null); onSelect(yearRange(y)); };
    const pickMonth = (m: string) => { if (year) { setMonth(m); onSelect(monthRange(year, m)); } };
    const pickDay = (d: string) => { if (year && month) onSelect(dayRange(year, month, d)); };

    const activeStart = captureRange?.start ?? '';

    let chips: React.ReactNode;
    if (!year) {
        chips = (
            <>
                <button type="button" className={`pt-timeline-chip${!captureRange ? ' on' : ''}`} onClick={reset}>All</button>
                {years.map((y) => (
                    <button key={y} type="button" className="pt-timeline-chip" onClick={() => pickYear(y)}>
                        {y}<span className="pt-timeline-count">{timeline.years[y].count}</span>
                    </button>
                ))}
            </>
        );
    } else if (!month) {
        const months = Object.keys(timeline.years[year]?.months ?? {}).sort();
        chips = months.map((m) => (
            <button
                key={m}
                type="button"
                className={`pt-timeline-chip${activeStart === `${year}-${m}-01` && captureRange?.end !== `${year}-${m}-01` ? ' on' : ''}`}
                onClick={() => pickMonth(m)}
            >
                {MONTHS[Number(m) - 1]}<span className="pt-timeline-count">{timeline.years[year].months[m].count}</span>
            </button>
        ));
    } else {
        const days = Object.keys(timeline.years[year]?.months[month]?.days ?? {}).sort();
        chips = days.map((d) => (
            <button
                key={d}
                type="button"
                className={`pt-timeline-chip${activeStart === `${year}-${month}-${d}` ? ' on' : ''}`}
                onClick={() => pickDay(d)}
            >
                {Number(d)}<span className="pt-timeline-count">{timeline.years[year].months[month].days[d]}</span>
            </button>
        ));
    }

    return (
        <div className="pt-timeline-wrap">
            <div className="pt-timeline-crumbs" aria-label="Timeline level">
                <button type="button" className={!year ? 'on' : undefined} onClick={reset}>All</button>
                {year && (<><span aria-hidden="true">›</span><button type="button" className={year && !month ? 'on' : undefined} onClick={() => pickYear(year)}>{year}</button></>)}
                {year && month && (<><span aria-hidden="true">›</span><button type="button" className="on" onClick={() => pickMonth(month)}>{MONTHS[Number(month) - 1]}</button></>)}
            </div>
            <div className="pt-timeline" role="group" aria-label="Jump to date">
                {chips}
            </div>
        </div>
    );
};

export default TimelineRail;
