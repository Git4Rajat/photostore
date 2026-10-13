import {isRawFilename, isVideoFilename} from '../utils/photoDisplay';
import {
    UPLOAD_PREVIEW_MAX_EDGE, UPLOAD_PREVIEW_MAX_BYTES, UPLOAD_THUMBNAIL_MAX_EDGE, UPLOAD_THUMBNAIL_MAX_BYTES,
    validUploadDerivative, type UploadDerivative,
} from '../utils/uploadDerivatives';

export type UploadPreparationResult = {
    status: 'generated'; preview: UploadDerivative; thumbnail: UploadDerivative;
} | {status: 'fallback'; reason: string};

type PreparationWorker = Pick<Worker, 'postMessage' | 'terminate' | 'onmessage' | 'onerror'>;
type WorkerFactory = (raw: boolean) => PreparationWorker;
interface PreparationOptions {signal?: AbortSignal; preserveEligibleJpeg?: boolean;}
interface Job {
    file: File; options: PreparationOptions; resolve: (result: UploadPreparationResult) => void;
    timer: ReturnType<typeof setTimeout>; abort: () => void; worker?: PreparationWorker; settled: boolean; deadline: number;
}
const createPreparationWorker: WorkerFactory = raw => raw
    ? new Worker(new URL('../workers/rawUploadAssets.worker.js', import.meta.url), {type: 'module'})
    : new Worker(new URL('../workers/rasterUploadAssets.worker.ts', import.meta.url), {type: 'module'});

// The caller starts this only after dedup/init, without awaiting it in a transfer lane.
export class UploadAssetPreparer {
    private queue: Job[] = [];
    private active: Job | null = null;
    private disposed = false;
    constructor(private workerFactory: WorkerFactory = createPreparationWorker, private maxQueued = 4, private timeoutMs = 30_000) {}

    prepare(file: File, options: PreparationOptions = {}): Promise<UploadPreparationResult> {
        if (this.disposed || options.signal?.aborted || isVideoFilename(file.name) || this.queue.length >= this.maxQueued) {
            return Promise.resolve({status: 'fallback', reason: this.disposed ? 'disposed' : options.signal?.aborted ? 'aborted' : isVideoFilename(file.name) ? 'unsupported' : 'busy'});
        }
        return new Promise(resolve => {
            const job = {file, options, resolve, settled: false, deadline: Date.now() + this.timeoutMs} as Job;
            job.abort = () => this.finish(job, {status: 'fallback', reason: 'aborted'});
            job.timer = setTimeout(() => this.finish(job, {status: 'fallback', reason: 'timeout'}), this.timeoutMs);
            options.signal?.addEventListener('abort', job.abort, {once: true});
            this.queue.push(job);
            this.pump();
        });
    }

    dispose(): void {
        this.disposed = true;
        for (const job of [...this.queue, ...(this.active ? [this.active] : [])]) this.finish(job, {status: 'fallback', reason: 'disposed'});
    }

    private finish(job: Job, result: UploadPreparationResult): void {
        if (job.settled) return;
        job.settled = true;
        clearTimeout(job.timer);
        job.options.signal?.removeEventListener('abort', job.abort);
        job.worker?.terminate();
        this.queue = this.queue.filter(item => item !== job);
        if (this.active === job) this.active = null;
        job.resolve(result);
        this.pump();
    }

    private pump(): void {
        if (this.disposed || this.active || !this.queue.length) return;
        const job = this.queue.shift()!;
        this.active = job;
        if (Date.now() >= job.deadline) {
            this.finish(job, {status: 'fallback', reason: 'timeout'});
            return;
        }
        try {
            const worker = this.workerFactory(isRawFilename(job.file.name));
            job.worker = worker;
            worker.onmessage = event => {
                const result = event.data;
                // RAW benchmark workers expose blobs separately from dimensions.
                const preview = result?.previewBlob ? {...result.preview, blob: result.previewBlob} : result?.preview;
                const thumbnail = result?.thumbnailBlob ? {...result.thumbnail, blob: result.thumbnailBlob} : result?.thumbnail;
                if (result?.status === 'generated'
                    && validUploadDerivative(preview, UPLOAD_PREVIEW_MAX_EDGE, UPLOAD_PREVIEW_MAX_BYTES)
                    && validUploadDerivative(thumbnail, UPLOAD_THUMBNAIL_MAX_EDGE, UPLOAD_THUMBNAIL_MAX_BYTES)) {
                    this.finish(job, {status: 'generated', preview, thumbnail});
                } else this.finish(job, {status: 'fallback', reason: 'generation_failed'});
            };
            worker.onerror = () => this.finish(job, {status: 'fallback', reason: 'generation_failed'});
            worker.postMessage({file: job.file, preserveEligibleJpeg: !!job.options.preserveEligibleJpeg, maxMegapixels: 120, maxBytes: 256 * 1024 * 1024});
        } catch {
            this.finish(job, {status: 'fallback', reason: 'unsupported'});
        }
    }
}
