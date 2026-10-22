import { postTools } from './apiClient';
import { acceptSourceEdits, pendingEditScope, sourceEditRequests } from './pendingEdits';

export const SOURCE_CHECK_TIMEOUT_MS = 3000;
export const SOURCE_CHECK_MAX_ENTITIES = 100;
type Check = { signature: string; at: number; request?: Promise<void>; rerun: boolean; cursor: number };
const checks = new Map<string, Check>();
const listeners = new Set<(scope: string) => void>();
export const subscribeSourceEdits = (listener: (scope: string) => void): (() => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
};

/** Bounded background work for unresolved edits and newly observed projection conflicts. */
export const reconcilePendingEdits = async (_fresh = false): Promise<void> => {
    const scope = pendingEditScope();
    const edits = sourceEditRequests(scope);
    if (!edits.length) return;
    const signature = JSON.stringify(edits);
    const previous = checks.get(scope);
    if (previous?.request) {
        // A later projection can invalidate an in-flight source read. Queue one follow-up.
        if (previous.signature !== signature) previous.rerun = true;
        return previous.request;
    }
    if (previous?.signature === signature && Date.now() - previous.at < 1000) return;
    const start = (previous?.cursor ?? 0) % edits.length;
    const rotated = [...edits.slice(start), ...edits.slice(0, start)];
    const selected = rotated.slice(0, SOURCE_CHECK_MAX_ENTITIES);
    const check: Check = { signature, at: 0, rerun: false, cursor: start + selected.length };
    checks.set(scope, check);
    check.request = (async () => {
        const controller = new AbortController();
        let expired = false;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const timeout = new Promise<void>(resolve => {
            timer = setTimeout(() => { expired = true; controller.abort(); resolve(); }, SOURCE_CHECK_TIMEOUT_MS);
        });
        const work = (async () => {
            try {
                for (let offset = 0; offset < selected.length; offset += 50) {
                    if (expired || scope !== pendingEditScope()) return;
                    const batch = selected.slice(offset, offset + 50);
                    const response = await postTools<{ libraryId: string; rows: Parameters<typeof acceptSourceEdits>[1] }>(
                        '/api/tools/pending-edits/reconcile', { edits: batch },
                        { signal: controller.signal, timeout: SOURCE_CHECK_TIMEOUT_MS, singleAttempt: true },
                    );
                    if (expired || scope !== pendingEditScope() || response.libraryId !== JSON.parse(scope)[1]) return;
                    if (acceptSourceEdits(batch, response.rows, scope)) {
                        listeners.forEach(listener => listener(scope));
                    }
                }
            } catch { /* Preserve overlays; the next refresh/poll retries unresolved work. */ }
        })();
        await Promise.race([work, timeout]);
        if (timer !== undefined) clearTimeout(timer);
        check.at = Date.now();
    })().finally(() => {
        check.request = undefined;
        if (check.rerun && scope === pendingEditScope()) {
            // Let this completed pass settle before starting work for the newer projection.
            queueMicrotask(() => { void reconcilePendingEdits(); });
        }
    });
    return check.request;
};

export const schedulePendingEditsReconciliation = (): void => { void reconcilePendingEdits().catch(() => undefined); };
