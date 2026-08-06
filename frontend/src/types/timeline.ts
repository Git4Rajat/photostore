// Mirrors backend/timeline_metadata.py's build_timeline_summary() response.
export interface TimelineDayBucket {
    count: number;
}

export interface TimelineMonthBucket {
    count: number;
    days: Record<string, number>; // "01".."31" -> count
}

export interface TimelineYearBucket {
    count: number;
    months: Record<string, TimelineMonthBucket>; // "01".."12"
}

export interface TimelineSummary {
    years: Record<string, TimelineYearBucket>; // "YYYY"
    cumulativeByYear: Record<string, number>;
    firstDate: string | null; // ISO date, e.g. "2018-04-02"
    lastDate: string | null;
    today: string; // ISO date, server's current day — timeline never navigates past this
    undatedCount: number;
    totalCount: number;
}
