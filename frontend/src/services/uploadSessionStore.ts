import type { PersistedUploadFile, PersistedUploadSession } from '../types/browserProcessing';

export const UPLOAD_CHECKPOINT_INTERVAL_MS = 2000;

// Own the live array so progress updates can replace one record without copying
// the whole queue or mutating file objects held by an active transfer lane.
export class UploadSessionStore {
    current: PersistedUploadSession | null = null;
    private positions = new Map<string, number>();
    private dirty = false;
    private timer: ReturnType<typeof setTimeout> | null = null;
    private checkpointPromise: Promise<boolean> | null = null;
    private resolveCheckpoint: ((saved: boolean) => void) | null = null;
    private saved = false;

    constructor(private storage: () => Storage, private storageKey: string) {}

    set(session: PersistedUploadSession | null): void {
        this.cancelTimer();
        this.settleCheckpoint(false);
        this.current = session ? { ...session, files: [...session.files] } : null;
        this.positions = new Map(this.current?.files.map((file, index) => [file.key, index]));
        this.dirty = true;
        this.flush();
    }

    update(key: string, updates: Partial<PersistedUploadFile>): void {
        const index = this.positions.get(key);
        if (!this.current || index === undefined) return;
        const merged = { ...this.current.files[index], ...updates };
        // A finished file no longer needs its resume-only fields: blockIds/
        // chunkSizeBytes exist solely to resume an interrupted chunked
        // transfer, and error/previewDataUrl are leftovers from an earlier
        // failed attempt before a successful retry. Across a large session
        // (hundreds-to-thousands of files) these accumulate in the JSON
        // blob written to localStorage and were pushing sessions over the
        // browser's per-origin quota, silently losing the resume checkpoint.
        // `merged` was just freshly allocated above and isn't aliased
        // anywhere else yet, so deleting in place skips a second full copy.
        if (merged.status === 'done') {
            delete merged.blockIds;
            delete merged.chunkSizeBytes;
            delete merged.error;
            delete merged.previewDataUrl;
        }
        this.current.files[index] = merged;
        this.dirty = true;
        this.schedule();
    }

    // A `done` file's entry is never read again (every consumer only filters
    // `done` files out) and the whole session already gets deleted once
    // nothing is left pending -- so dropping it the moment it's done costs
    // nothing that isn't already lost at end-of-session, while keeping a
    // large wave's checkpoint bounded by its in-flight/failed working set
    // instead of its total size. Swap-and-pop keeps `current.files` a real
    // array (no splice/reindex), preserving the zero-copy-on-progress
    // behavior other updates rely on for untouched entries.
    remove(key: string): void {
        const index = this.positions.get(key);
        if (!this.current || index === undefined) return;
        const files = this.current.files;
        const last = files.length - 1;
        if (index !== last) {
            files[index] = files[last];
            this.positions.set(files[index].key, index);
        }
        files.pop();
        this.positions.delete(key);
        this.dirty = true;
        this.schedule();
    }

    // Finalized source bytes must outlive the checkpoint that marks them done.
    // All completions in a window share this promise, without holding transfer lanes.
    checkpoint(): Promise<boolean> {
        if (!this.dirty) return Promise.resolve(this.saved);
        if (!this.checkpointPromise) {
            this.checkpointPromise = new Promise((resolve) => { this.resolveCheckpoint = resolve; });
        }
        this.schedule();
        return this.checkpointPromise;
    }

    flush(): boolean {
        this.cancelTimer();
        if (this.dirty) {
            try {
                const storage = this.storage();
                if (this.current) storage.setItem(this.storageKey, JSON.stringify(this.current));
                else storage.removeItem(this.storageKey);
                this.saved = true;
                this.dirty = false;
            } catch {
                // Quota/private-mode failures must not stop this tab's uploads.
                this.saved = false;
            }
        }
        this.settleCheckpoint(this.saved);
        return this.saved;
    }

    private schedule(): void {
        // Fixed from the first update, not a debounce that can starve forever.
        if (this.timer === null) this.timer = setTimeout(() => this.flush(), UPLOAD_CHECKPOINT_INTERVAL_MS);
    }

    private cancelTimer(): void {
        if (this.timer !== null) clearTimeout(this.timer);
        this.timer = null;
    }

    private settleCheckpoint(saved: boolean): void {
        const resolve = this.resolveCheckpoint;
        this.resolveCheckpoint = null;
        this.checkpointPromise = null;
        resolve?.(saved);
    }
}
