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
        this.current.files[index] = { ...this.current.files[index], ...updates };
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
