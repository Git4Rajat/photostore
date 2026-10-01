import { ApiError, classifyApiError } from './apiError';

// One shared, app-wide FIFO for every non-critical background request (tab
// prefetches, index warm-ups, and similar "nice to have before the user
// needs it" calls). At most one of these runs at a time, across the whole
// app -- not per caller -- which is the point: before this existed,
// StoreProvider fired 6+ independent fetches in the same mount tick,
// several index downloads raced each other, and a handful of flat-interval
// pollers kept hammering a struggling backend at full speed regardless of
// whether it was recovering. See the 2026-10-01 microsvcpoc-dev crash-loop
// investigation this was built to address.
//
// A failed item doesn't retry in place -- it goes to the BACK of the queue
// instead, so one struggling endpoint can't starve everything behind it,
// and a failing item naturally gets more time between attempts as the queue
// grows (an implicit backoff with no separate timer needed). A terminal
// failure (4xx, or the caller's own AbortSignal firing) drops the item
// instead of requeuing it -- retrying an auth/not-found error or a
// deliberately-abandoned request just burns a queue slot.
//
// Callers MUST pass their own request's `signal` through to `get`/`post`
// *and* set `singleAttempt: true` on that call's config (see
// httpClient.ts's RequestConfig) -- this queue is the sole retry authority
// for anything it manages. Leaving httpClient's own ~90s cold-start retry
// loop active underneath it would mean a "failure" this queue reacts to is
// actually hiding up to 14 retries, stacking two retry layers and
// compounding load on exactly the backend this queue exists to protect.

interface QueueItem<T> {
    task: (signal: AbortSignal) => Promise<T>;
    signal: AbortSignal;
    attempts: number;
    maxAttempts: number;
    resolve: (value: T) => void;
    reject: (error: unknown) => void;
}

// Shared by callers that don't need their own cancellation scope (e.g.
// boot-critical fetches tied to the whole app session, not a specific tab) --
// a signal that is simply never aborted.
const NEVER_ABORTED = new AbortController().signal;

const DEFAULT_MAX_ATTEMPTS = 8;

const queue: QueueItem<unknown>[] = [];
let processing = false;

const makeAbortError = (): ApiError => new ApiError({
    kind: 'canceled',
    message: 'Request canceled.',
    rawMessage: 'Aborted before its turn in the background request queue.',
    retriable: false,
    requestId: Math.random().toString(36).slice(2, 6),
});

async function pump(): Promise<void> {
    if (processing) {
        return;
    }
    processing = true;
    try {
        while (queue.length > 0) {
            const item = queue.shift() as QueueItem<unknown>;
            if (item.signal.aborted) {
                item.reject(makeAbortError());
                continue;
            }
            item.attempts += 1;
            try {
                // eslint-disable-next-line no-await-in-loop -- the whole point: exactly one in flight at a time.
                const result = await item.task(item.signal);
                item.resolve(result);
            } catch (error) {
                if (item.signal.aborted) {
                    item.reject(makeAbortError());
                    continue;
                }
                const classified = classifyApiError(error);
                if (classified.kind === 'canceled') {
                    item.reject(classified);
                    continue;
                }
                if (classified.retriable && item.attempts < item.maxAttempts) {
                    queue.push(item); // back of the queue, not retried in place
                    continue;
                }
                item.reject(classified);
            }
        }
    } finally {
        processing = false;
    }
}

export interface EnqueueOptions {
    // Ties this item's lifetime to a caller-owned scope (typically a tab's
    // mount, via an AbortController aborted in the component's unmount
    // cleanup). An item still queued when its signal fires is dropped
    // without ever running; an item already in flight has that signal
    // passed through to its task, so the underlying request aborts too.
    // Omit for work tied to the whole app session rather than one tab.
    signal?: AbortSignal;
    // Ceiling on requeue-to-back attempts for a retryable failure, after
    // which the item is dropped and its promise rejects with the last
    // error -- so a backend that's down for a very long time doesn't keep
    // an abandoned item cycling through the queue forever.
    maxAttempts?: number;
}

// Enqueues `task` behind everything already queued and returns a promise
// that settles once it finally succeeds, is dropped as a terminal failure,
// or exhausts maxAttempts. `task` receives the same AbortSignal passed via
// `options.signal` (or the shared never-aborted one) -- pass it straight
// through to the underlying get/post call's config so an in-flight request
// is actually canceled, not just ignored, when the caller navigates away.
export function enqueueBackgroundRequest<T>(
    task: (signal: AbortSignal) => Promise<T>,
    options: EnqueueOptions = {},
): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        const item: QueueItem<T> = {
            task,
            signal: options.signal ?? NEVER_ABORTED,
            attempts: 0,
            maxAttempts: options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS,
            resolve,
            reject,
        };
        queue.push(item as QueueItem<unknown>);
        void pump();
    });
}

// Test/debug only -- current queue depth (items not yet started or
// requeued after a retryable failure).
export const _backgroundRequestQueueLength = (): number => queue.length;
