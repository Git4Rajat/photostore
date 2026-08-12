// Client side of the background-job completion notifier. Long-running jobs
// (reclustering, "find more faces", library cleanup, preview generation) run on
// the worker and record their lifecycle in the backend `jobs` table. This
// module lets the app poll `/api/jobs/status` and surface completions in the
// notification bell + a toast. See AppServicesProvider for the poll loop.
import { get } from './apiClient';
import { isAuthEnabled, getActiveAccount } from './authClient';
import { hasValidSession } from './passwordAuthClient';

export type JobStatusRecord = {
    jobId: string;
    // 'queued' | 'running' | 'done' | 'failed' | 'unknown'
    status: string;
    // 'recluster' | 'find_faces' | 'cluster' | 'library_clean' | 'preview' | 'job'
    kind: string;
    title: string;
    message: string;
    error?: string | null;
    updatedAt?: string;
    suppressNotification?: boolean;
};

export const TERMINAL_JOB_STATUSES = new Set(['done', 'failed']);

// Job kinds worth interrupting the user with a toast. Everything still lands in
// the notification bell; previews are frequent + low-signal so they stay quiet.
export const TOASTABLE_JOB_KINDS = new Set(['recluster', 'find_faces', 'cluster', 'library_clean', 'job']);

// People-clustering job kinds. While any of these is queued/running we surface an
// in-progress indicator (People-page banner + global pill) so the user isn't
// blind to clustering happening on the worker.
export const CLUSTERING_JOB_KINDS = new Set(['cluster', 'recluster', 'find_faces']);

export const isClusteringJob = (job: JobStatusRecord): boolean => CLUSTERING_JOB_KINDS.has(job.kind);

// Human label for the in-progress clustering indicator, given the in-flight jobs.
// Grouping (initial cluster / recluster) is the dominant signal; "find more
// faces" gets its own wording. Returns '' when no clustering job is in flight.
export const clusteringActivityLabel = (jobs: JobStatusRecord[]): string => {
    const clustering = jobs.filter((job) => isClusteringJob(job) && !TERMINAL_JOB_STATUSES.has(job.status));
    if (clustering.length === 0) {
        return '';
    }
    if (clustering.some((job) => job.kind === 'cluster' || job.kind === 'recluster')) {
        return 'Grouping people…';
    }
    return 'Finding more faces…';
};

// ipwork runs one job row per photo (backend/both processing mode), so it's
// excluded from the notification bell/toast (see the kind !== 'ipwork' check
// around pollJobStatusesOnce) to avoid one toast per file on a bulk upload.
// That left no global signal that server-side processing was happening at
// all — this label backs a standalone indicator for exactly that gap.
export const isIpworkJob = (job: JobStatusRecord): boolean => job.kind === 'ipwork';

// Returns '' when no ipwork job is in flight. /api/jobs/status caps its
// response at 50 rows, so a large backlog can't be counted reliably from this
// list alone — keep the label a simple presence signal rather than a count
// that would silently undercount once the backlog exceeds the cap.
export const ipworkActivityLabel = (jobs: JobStatusRecord[]): string => {
    const inFlight = jobs.some((job) => isIpworkJob(job) && !TERMINAL_JOB_STATUSES.has(job.status));
    return inFlight ? 'Processing photos…' : '';
};

// Only announce a completion if it finished within this window. Guards against
// re-notifying for jobs that wrapped up long before the app was opened (the
// backend already bounds the payload, this is a tighter client-side gate).
export const JOB_NOTIFY_WINDOW_MS = 15 * 60 * 1000;

export const fetchJobStatuses = async (): Promise<JobStatusRecord[]> => {
    const response = await get<{ jobs?: JobStatusRecord[] }>('/api/jobs/status');
    const jobs = response?.jobs;
    return Array.isArray(jobs) ? jobs : [];
};

// Cheap "should we even poll?" gate so we don't hammer the API on the login
// screen. When auth is disabled entirely (local/dev) we always allow it.
export const isLikelyAuthenticated = (): boolean => {
    try {
        if (!isAuthEnabled()) {
            return true;
        }
    } catch {
        /* ignore */
    }
    try {
        if (hasValidSession()) {
            return true;
        }
    } catch {
        /* ignore */
    }
    try {
        if (getActiveAccount()) {
            return true;
        }
    } catch {
        /* ignore */
    }
    return false;
};

// --- Persisted "already announced" set --------------------------------------
// Kept in localStorage so a page reload doesn't re-announce a job we already
// showed. Bounded so it can't grow without limit.
const SEEN_STORAGE_KEY = 'photostore.jobNotifier.seen';
const SEEN_MAX = 200;

export const loadSeenJobIds = (): Set<string> => {
    try {
        const raw = localStorage.getItem(SEEN_STORAGE_KEY);
        if (!raw) {
            return new Set();
        }
        const parsed = JSON.parse(raw);
        return new Set(Array.isArray(parsed) ? parsed.map(String) : []);
    } catch {
        return new Set();
    }
};

export const persistSeenJobIds = (seen: Set<string>): void => {
    try {
        // Keep only the most recent ids to stay bounded.
        const ids = Array.from(seen).slice(-SEEN_MAX);
        localStorage.setItem(SEEN_STORAGE_KEY, JSON.stringify(ids));
    } catch {
        /* ignore */
    }
};

// --- Poll nudges ------------------------------------------------------------
// Job trigger sites (recluster button, "find more faces", etc.) call
// requestJobPoll() right after enqueuing so the notifier wakes immediately
// instead of waiting for the next focus/mount.
const pollListeners = new Set<() => void>();

export const onJobPollRequested = (listener: () => void): (() => void) => {
    pollListeners.add(listener);
    return () => {
        pollListeners.delete(listener);
    };
};

export const requestJobPoll = (): void => {
    pollListeners.forEach((listener) => {
        try {
            listener();
        } catch {
            /* ignore */
        }
    });
};
