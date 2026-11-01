export type UploadWorker = Pick<Worker, 'postMessage' | 'terminate' | 'onmessage' | 'onerror'>;

export function discardUploadWorker(worker: UploadWorker): void {
    worker.onmessage = null;
    worker.onerror = null;
    worker.terminate();
}

// Admission/concurrency belongs to the caller. Retain only a small number of
// healthy, idle workers; decoder failures and cancelled jobs are never reused.
export class UploadWorkerReuse {
    private idle: Array<{worker: UploadWorker; timer: ReturnType<typeof setTimeout>}> = [];
    private disposed = false;
    constructor(private maxIdle = 2, private idleMs = 15_000) {}

    take(create: () => UploadWorker): UploadWorker {
        if (this.disposed) throw new Error('Upload worker pool disposed');
        const entry = this.idle.pop();
        if (!entry) return create();
        clearTimeout(entry.timer);
        entry.worker.onerror = null;
        return entry.worker;
    }

    release(worker: UploadWorker): void {
        worker.onmessage = null;
        worker.onerror = null;
        if (this.disposed || this.maxIdle <= 0) { discardUploadWorker(worker); return; }
        if (this.idle.length >= this.maxIdle) {
            const oldest = this.idle.shift()!;
            clearTimeout(oldest.timer);
            discardUploadWorker(oldest.worker);
        }
        const entry = {worker, timer: setTimeout(() => {
            this.idle = this.idle.filter(item => item !== entry);
            discardUploadWorker(worker);
        }, this.idleMs)};
        worker.onerror = () => {
            clearTimeout(entry.timer);
            this.idle = this.idle.filter(item => item !== entry);
            discardUploadWorker(worker);
        };
        this.idle.push(entry);
    }

    dispose(): void {
        this.disposed = true;
        for (const {worker, timer} of this.idle) {
            clearTimeout(timer);
            discardUploadWorker(worker);
        }
        this.idle = [];
    }
}
