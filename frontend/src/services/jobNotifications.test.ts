import { describe, expect, it } from 'vitest';
import { ipworkActivityLabel, clusteringActivityLabel, jobNotificationKey, indexNotificationGroup, TERMINAL_JOB_STATUSES, type JobStatusRecord } from './jobNotifications';

const job = (overrides: Partial<JobStatusRecord>): JobStatusRecord => ({
    jobId: 'job-1',
    status: 'done',
    kind: 'ipwork',
    title: '',
    message: '',
    ...overrides,
});

it('distinguishes later index completions even though the job ID is reused', () => {
    const first = job({kind: 'index_build', updatedAt: '2026-10-15T10:00:00Z'});
    expect(jobNotificationKey(first)).not.toBe(jobNotificationKey({...first, updatedAt: '2026-10-15T10:01:00Z'}));
    expect(jobNotificationKey(first)).toBe(jobNotificationKey({...first}));
    expect(jobNotificationKey(job({kind: 'cluster', updatedAt: 'later'}))).toBe('job-1');
});

it('groups both duplicate-check passes but keeps scopes and failures distinct', () => {
    const dedup = job({kind: 'index_build', indexScope: 'dedup'});
    expect(indexNotificationGroup(dedup)).toBe(indexNotificationGroup({...dedup, indexScope: 'dedup-idle'}));
    expect(indexNotificationGroup(dedup)).not.toBe(indexNotificationGroup({...dedup, indexScope: 'light'}));
    expect(indexNotificationGroup(dedup)).not.toBe(indexNotificationGroup({...dedup, status: 'failed'}));
    expect(indexNotificationGroup(job({}))).toBeUndefined();
    const scoped = {...dedup, jobId: 'index-build-library-a-dedup'};
    expect(indexNotificationGroup(scoped)).toBe(indexNotificationGroup({...scoped,
        jobId: 'index-build-library-a-dedup-idle', indexScope: 'dedup-idle'}));
    expect(indexNotificationGroup(scoped)).not.toBe(indexNotificationGroup({...scoped, jobId: 'index-build-library-b-dedup'}));
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
