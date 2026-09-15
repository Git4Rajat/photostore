import { describe, expect, it } from 'vitest';
import { ipworkActivityLabel, clusteringActivityLabel, TERMINAL_JOB_STATUSES, type JobStatusRecord } from './jobNotifications';

const job = (overrides: Partial<JobStatusRecord>): JobStatusRecord => ({
    jobId: 'job-1',
    status: 'done',
    kind: 'ipwork',
    title: '',
    message: '',
    ...overrides,
});

describe('TERMINAL_JOB_STATUSES', () => {
    it('treats skipped as terminal, same as done/failed', () => {
        expect(TERMINAL_JOB_STATUSES.has('skipped')).toBe(true);
        expect(TERMINAL_JOB_STATUSES.has('done')).toBe(true);
        expect(TERMINAL_JOB_STATUSES.has('failed')).toBe(true);
        expect(TERMINAL_JOB_STATUSES.has('running')).toBe(false);
        expect(TERMINAL_JOB_STATUSES.has('queued')).toBe(false);
    });
});

describe('ipworkActivityLabel', () => {
    it('does not show "Processing photos…" when every ipwork job is skipped', () => {
        // 'both'-mode racing produces a steady stream of skipped/already_done
        // ipwork jobs on an otherwise fully idle backend (2026-09-14 live
        // incident: pill stayed lit for up to an hour after all work finished).
        const jobs = [
            job({ jobId: 'a', status: 'skipped' }),
            job({ jobId: 'b', status: 'skipped' }),
        ];
        expect(ipworkActivityLabel(jobs)).toBe('');
    });

    it('still shows the label while an ipwork job is genuinely running', () => {
        const jobs = [job({ jobId: 'a', status: 'running' })];
        expect(ipworkActivityLabel(jobs)).toBe('Processing photos…');
    });

    it('ignores non-ipwork jobs', () => {
        const jobs = [job({ jobId: 'a', kind: 'preview', status: 'running' })];
        expect(ipworkActivityLabel(jobs)).toBe('');
    });
});

describe('clusteringActivityLabel', () => {
    it('is unaffected by ipwork-only skipped status (clustering jobs never use it)', () => {
        const jobs = [job({ jobId: 'a', kind: 'cluster', status: 'running' })];
        expect(clusteringActivityLabel(jobs)).toBe('Grouping people…');
    });

    it('returns empty when the only clustering job is done', () => {
        const jobs = [job({ jobId: 'a', kind: 'cluster', status: 'done' })];
        expect(clusteringActivityLabel(jobs)).toBe('');
    });
});
