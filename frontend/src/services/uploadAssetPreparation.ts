import {isHeicFilename, isRawFilename, isVideoFilename} from '../utils/photoDisplay';
import {preparationFailureReason} from '../utils/uploadPreparationDiagnostics';
import {
    UPLOAD_PREVIEW_MAX_EDGE, UPLOAD_PREVIEW_MAX_BYTES, UPLOAD_THUMBNAIL_MAX_EDGE, UPLOAD_THUMBNAIL_MAX_BYTES,
    validUploadDerivative, type UploadDerivative,
} from '../utils/uploadDerivatives';

export type UploadPreparationResult = {
    status: 'generated'; preview?: UploadDerivative; thumbnail: UploadDerivative;
    runtime?: 'embedded_video_jpeg' | 'native_video' | 'ffmpeg_wasm'; readBytes?: number; readCalls?: number;
} | {status: 'fallback'; reason: string; failureReason?: string};

type PreparationWorker = Pick<Worker, 'postMessage' | 'terminate' | 'onmessage' | 'onerror'>;
type WorkerFactory = (raw: boolean, heif?: boolean) => PreparationWorker;
interface PreparationOptions {signal?: AbortSignal; preserveEligibleJpeg?: boolean;}
interface Job {
    file: File; options: PreparationOptions; resolve: (result: UploadPreparationResult) => void;
    timer?: ReturnType<typeof setTimeout>; abort: () => void; worker?: PreparationWorker; settled: boolean; controller: AbortController;
    retried: boolean;
}

// Only reasons plausibly caused by transient resource contention (another queued
// job hogging the main/worker thread, a flaky WASM/OffscreenCanvas init) are worth
// retrying. Anything else (unsupported format, resource-guard rejection, invalid
// output) is deterministic -- retrying just delays the backend fallback by another
// timeoutMs for a guaranteed repeat failure.
const RETRYABLE_FAILURE_REASONS = new Set(['generation_timeout', 'worker_error']);
const createPreparationWorker: WorkerFactory = (raw, heif = false) => raw
    ? new Worker(new URL('../workers/rawUploadAssets.worker.js', import.meta.url), {type: 'module'})
    : heif ? new Worker(new URL('../workers/heifUploadAssets.worker.ts', import.meta.url), {type: 'module'})
    : new Worker(new URL('../workers/rasterUploadAssets.worker.ts', import.meta.url), {type: 'module'});

// The caller starts this only after dedup/init, without awaiting it in a transfer lane.
export class UploadAssetPreparer {
    private queue: Job[] = [];
    private active = new Set<Job>();
    private disposed = false;
    // Previously only one decoder ran at a time, so in a large batch a file near
    // the back of the queue could burn most of its own 30s budget just waiting
    // for its turn -- not because it, or its format, was slow. Running a small
    // number of decoders concurrently cuts that queue wait roughly proportionally,
    // leaving each job more of its budget for actual decode time. Kept modest
    // (not scaled to hardwareConcurrency) because each RAW/HEIC decode is a
    // memory-heavy WASM instance, and mobile Safari has tighter per-tab memory
    // limits than desktop.
    constructor(
        private workerFactory: WorkerFactory = createPreparationWorker, private maxQueued = 4,
        private timeoutMs = 30_000, private maxActive = 2,
    ) {}

    prepare(file: File, options: PreparationOptions = {}): Promise<UploadPreparationResult> {
        // maxQueued bounds work waiting for a slot, not total in-flight capacity --
        // active jobs no longer always number exactly 1, so the busy check has to
        // account for maxActive too or a concurrency bump silently raises how much
        // work callers can queue up beyond what they sized their admission gate for.
        if (this.disposed || options.signal?.aborted || this.active.size + this.queue.length >= this.maxActive + this.maxQueued) {
            return Promise.resolve({status: 'fallback', reason: this.disposed ? 'disposed' : options.signal?.aborted ? 'aborted' : 'busy'});
        }
        return new Promise(resolve => {
            const job = {file, options, resolve, settled: false, retried: false, controller: new AbortController()} as Job;
            job.abort = () => this.finish(job, {status: 'fallback', reason: 'aborted'});
            options.signal?.addEventListener('abort', job.abort, {once: true});
            this.queue.push(job);
            this.pump();
        });
    }

    dispose(): void {
        this.disposed = true;
        for (const job of [...this.queue, ...Array.from(this.active)]) this.finish(job, {status: 'fallback', reason: 'disposed'});
    }

    private finish(job: Job, result: UploadPreparationResult): void {
        if (job.settled) return;
        job.settled = true;
        clearTimeout(job.timer);
        job.options.signal?.removeEventListener('abort', job.abort);
        job.worker?.terminate();
        job.controller.abort();
        this.queue = this.queue.filter(item => item !== job);
        this.active.delete(job);
        job.resolve(result);
        this.pump();
    }

    private pump(): void {
        if (this.disposed) return;
        while (this.active.size < this.maxActive && this.queue.length) {
            const job = this.queue.shift()!;
            this.active.add(job);
            this.runJob(job);
        }
    }

    // Retries exactly once, only for failure reasons that plausibly stem from
    // transient contention rather than a deterministic decode/format problem.
    private retryOrFinish(job: Job, result: UploadPreparationResult): void {
        if (job.settled) return;
        if (result.status === 'fallback' && !job.retried && RETRYABLE_FAILURE_REASONS.has(result.failureReason ?? '')) {
            job.retried = true;
            clearTimeout(job.timer);
            job.worker?.terminate();
            job.worker = undefined;
            this.runJob(job);
            return;
        }
        this.finish(job, result);
    }

    private runJob(job: Job): void {
        job.timer = setTimeout(() => this.retryOrFinish(job, {status: 'fallback', reason: 'timeout',
            failureReason: 'generation_timeout'}), this.timeoutMs);
        if (isVideoFilename(job.file.name)) {
            import('./videoThumbnailPreparation').then(module => module.prepareVideoThumbnail(job.file, job.controller.signal))
                .then(result => {
                    if (!validUploadDerivative(result.thumbnail, UPLOAD_THUMBNAIL_MAX_EDGE, UPLOAD_THUMBNAIL_MAX_BYTES)) throw new Error('Invalid video derivative');
                    this.finish(job, {status: 'generated', ...result});
                }).catch(error => this.retryOrFinish(job, {status: 'fallback', reason: 'generation_failed', failureReason: preparationFailureReason(error)}));
            return;
        }
        try {
            const worker = isHeicFilename(job.file.name) ? this.workerFactory(false, true) : this.workerFactory(isRawFilename(job.file.name));
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
                } else this.retryOrFinish(job, {status: 'fallback', reason: 'generation_failed',
                    failureReason: result?.status === 'generated' ? 'invalid_derivative' : result?.failureReason || preparationFailureReason(result?.error)});
            };
            worker.onerror = () => this.retryOrFinish(job, {status: 'fallback', reason: 'generation_failed', failureReason: 'worker_error'});
            worker.postMessage({file: job.file, preserveEligibleJpeg: job.options.preserveEligibleJpeg !== false, maxMegapixels: 120, maxBytes: 256 * 1024 * 1024});
        } catch {
            this.finish(job, {status: 'fallback', reason: 'unsupported', failureReason: 'worker_start_failed'});
        }
    }
}
