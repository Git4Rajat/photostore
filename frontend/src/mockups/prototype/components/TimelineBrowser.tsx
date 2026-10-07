import React from 'react';
import type { CaptureRange, TimelineSummary } from '../store';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const pad = (n: number | string) => String(n).padStart(2, '0');
const lastDayOf = (year: string, month: string) => new Date(Number(year), Number(month), 0).getDate();

export const yearRange = (y: string): CaptureRange => ({ start: `${y}-01-01`, end: `${y}-12-31`, label: y });
export const monthRange = (y: string, m: string): CaptureRange => ({
    start: `${y}-${m}-01`, end: `${y}-${m}-${pad(lastDayOf(y, m))}`, label: `${MONTHS[Number(m) - 1]} ${y}`,
});

/**
 * The Months / Years "zoomed-out" gallery levels (iOS Photos style). Instead of
 * a separate scrubber rail, the user keeps pinching/zooming out from the photo
 * grid into Months, then Years; tapping a card drills back down into that date
 * range. Counts come straight from /photos/timeline so they match the grid.
 */
export const TimelineBrowser: React.FC<{
    timeline: TimelineSummary;
    level: 'months' | 'years';
    focusYear: string | null;
    onOpenYear: (year: string) => void;
    onOpenMonth: (range: CaptureRange, year: string) => void;
}> = ({ timeline, level, focusYear, onOpenYear, onOpenMonth }) => {
    const years = Object.keys(timeline.years).sort().reverse();

    if (level === 'years') {
        return (
            <div className="pt-period-grid" role="group" aria-label="Years">
                {years.map((y, i) => (
                    <button key={y} type="button" className={`pt-period-card mock-swatch s${(i % 8) + 1}`} onClick={() => onOpenYear(y)}>
                        {timeline.years[y].coverThumbnailUrl && <img className="pt-period-cover" src={timeline.years[y].coverThumbnailUrl} alt="" loading="lazy" />}
                        <span className="pt-period-label">{y}</span>
                        <span className="pt-period-count">{timeline.years[y].count.toLocaleString()} photo{timeline.years[y].count === 1 ? '' : 's'}</span>
                    </button>
                ))}
                {years.length === 0 && <p className="pt-grid-empty">No dated photos yet.</p>}
            </div>
        );
    }

    const yearsToShow = focusYear ? (timeline.years[focusYear] ? [focusYear] : []) : years;
    const entries: { y: string; m: string; count: number; coverThumbnailUrl?: string }[] = [];
    for (const y of yearsToShow) {
        const months = Object.keys(timeline.years[y].months).sort().reverse();
        for (const m of months) {
            const month = timeline.years[y].months[m];
            entries.push({ y, m, count: month.count, coverThumbnailUrl: month.coverThumbnailUrl });
        }
    }

    return (
        <div className="pt-period-grid" role="group" aria-label="Months">
            {entries.map(({ y, m, count, coverThumbnailUrl }, i) => (
                <button
                    key={`${y}-${m}`}
                    type="button"
                    className={`pt-period-card mock-swatch s${(i % 8) + 1}`}
                    onClick={() => onOpenMonth(monthRange(y, m), y)}
                >
                    {coverThumbnailUrl && <img className="pt-period-cover" src={coverThumbnailUrl} alt="" loading="lazy" />}
                    <span className="pt-period-label">{MONTHS[Number(m) - 1]}{focusYear ? '' : ` ${y}`}</span>
                    <span className="pt-period-count">{count.toLocaleString()} photo{count === 1 ? '' : 's'}</span>
                </button>
            ))}
            {entries.length === 0 && <p className="pt-grid-empty">No months to show.</p>}
        </div>
    );
};

export default TimelineBrowser;
