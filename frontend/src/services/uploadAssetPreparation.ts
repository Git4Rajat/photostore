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
}
const createPreparationWorker: WorkerFactory = (raw, heif = false) => raw
    ? new Worker(new URL('../workers/rawUploadAssets.worker.js', import.meta.url), {type: 'module'})
    : heif ? new Worker(new URL('../workers/heifUploadAssets.worker.ts', import.meta.url), {type: 'module'})
    : new Worker(new URL('../workers/rasterUploadAssets.worker.ts', import.meta.url), {type: 'module'});

// The caller starts this only after dedup/init, without awaiting it in a transfer lane.
export class UploadAssetPreparer {
    private queue: Job[] = [];
    private active: Job | null = null;
    private disposed = false;
    constructor(private workerFactory: WorkerFactory = createPreparationWorker, private maxQueued = 4, private timeoutMs = 30_000) {}

    prepare(file: File, options: PreparationOptions = {}): Promise<UploadPreparationResult> {
        if (this.disposed || options.signal?.aborted || this.queue.length >= this.maxQueued) {
            return Promise.resolve({status: 'fallback', reason: this.disposed ? 'disposed' : options.signal?.aborted ? 'aborted' : 'busy'});
        }
        return new Promise(resolve => {
            const job = {file, options, resolve, settled: false, controller: new AbortController()} as Job;
            job.abort = () => this.finish(job, {status: 'fallback', reason: 'aborted'});
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
        job.controller.abort();
        this.queue = this.queue.filter(item => item !== job);
        if (this.active === job) this.active = null;
        job.resolve(result);
        this.pump();
    }

    private pump(): void {
        if (this.disposed || this.active || !this.queue.length) return;
        const job = this.queue.shift()!;
        this.active = job;
        job.timer = setTimeout(() => this.finish(job, {status: 'fallback', reason: 'timeout',
            failureReason: 'generation_timeout'}), this.timeoutMs);
        if (isVideoFilename(job.file.name)) {
            import('./videoThumbnailPreparation').then(module => module.prepareVideoThumbnail(job.file, job.controller.signal))
                .then(result => {
                    if (!validUploadDerivative(result.thumbnail, UPLOAD_THUMBNAIL_MAX_EDGE, UPLOAD_THUMBNAIL_MAX_BYTES)) throw new Error('Invalid video derivative');
                    this.finish(job, {status: 'generated', ...result});
                }).catch(error => this.finish(job, {status: 'fallback', reason: 'generation_failed', failureReason: preparationFailureReason(error)}));
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
                } else this.finish(job, {status: 'fallback', reason: 'generation_failed',
                    failureReason: result?.status === 'generated' ? 'invalid_derivative' : result?.failureReason || preparationFailureReason(result?.error)});
            };
            worker.onerror = () => this.finish(job, {status: 'fallback', reason: 'generation_failed', failureReason: 'worker_error'});
            worker.postMessage({file: job.file, preserveEligibleJpeg: job.options.preserveEligibleJpeg !== false, maxMegapixels: 120, maxBytes: 256 * 1024 * 1024});
        } catch {
            this.finish(job, {status: 'fallback', reason: 'unsupported', failureReason: 'worker_start_failed'});
        }
    }
}
