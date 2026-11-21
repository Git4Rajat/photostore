// How long to collect files before sending one /upload/finalize-batch call.
//
// Every finalize call takes a turn on the same shared wave record on the
// server, so the cost that grows under load is the number of calls, not the
// number of files. While the server is busy (a call is already in flight, or
// the last one was slow) collect longer so each call carries more files;
// while it is idle, keep the short window so a small upload never waits.

export const FINALIZE_BATCH_MAX_SIZE = 24;

const IDLE = { collectMs: 100, maxWaitMs: 500 };
const BUSY = { collectMs: 1000, maxWaitMs: 2000 };

// A call slower than this means the shared record is contended.
export const FINALIZE_SLOW_CALL_MS = 3000;

export interface FinalizeBatchLoad {
    inFlight: number;
    lastCallMs: number | null;
}

export function finalizeBatchWindow(load: FinalizeBatchLoad): { collectMs: number; maxWaitMs: number } {
    const inFlight = Number.isFinite(load.inFlight) ? load.inFlight : 0;
    const slow = load.lastCallMs !== null && Number.isFinite(load.lastCallMs) && load.lastCallMs >= FINALIZE_SLOW_CALL_MS;
    return inFlight > 0 || slow ? BUSY : IDLE;
}
