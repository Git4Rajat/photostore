import {UploadAssetPreparer} from './uploadAssetPreparation';
import type {UploadMetadataDescriptor, UploadMetadataPacket} from '../utils/uploadMetadata';
import {isVideoFilename} from '../utils/photoDisplay';
import {extractBrowserExif} from '../utils/browserExifTool';
import {PREPARATION_ASSETS, type AssetPreparationDiagnostic, type PreparationAsset, type UploadPreparationReport} from '../utils/uploadPreparationDiagnostics';

export interface PreparedUpload {
    clientProcessing?: Record<string, any>;
    clientProcessingReport?: Array<Record<string, unknown>>;
    clientMetadata?: UploadMetadataDescriptor;
    preparationReport?: UploadPreparationReport;
}
export interface UploadAssetTargets {previewBlobUrl?: string; thumbnailBlobUrl?: string; metadataBlobUrl?: string; uploadPreparationEnabled?: boolean; metadataPreparationReason?: string;}

// Queued entries retain only File references; decoding and PUTs remain bounded.
class PreparationGate {
    private active = 0;
    private waiting: Array<{priority: number; grant: () => void}> = [];
    constructor(private limit: number) {}

    acquire(signal: AbortSignal, priority = 0): Promise<(() => void) | null> {
        if (signal.aborted) return Promise.resolve(null);
        return new Promise(resolve => {
            const entry = {priority, grant: () => {
                signal.removeEventListener('abort', abort);
                this.active++;
                let released = false;
                resolve(() => {
                    if (released) return;
                    released = true;
                    this.active--;
                    this.waiting.shift()?.grant();
                });
            }};
            const abort = () => {
                this.waiting = this.waiting.filter(item => item !== entry);
                resolve(null);
            };
            if (this.active < this.limit) entry.grant();
            else {
                signal.addEventListener('abort', abort, {once: true});
                const index = this.waiting.findIndex(item => item.priority < priority);
                this.waiting.splice(index < 0 ? this.waiting.length : index, 0, entry);
            }
        });
    }
}

const delay = (ms: number, signal: AbortSignal): Promise<void> => new Promise(resolve => {
    const done = () => { clearTimeout(timer); signal.removeEventListener('abort', done); resolve(); };
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', done, {once: true});
    if (signal.aborted) done();
});

export class UploadPreparationPipeline {
    private preparer = new UploadAssetPreparer();
    private jobs = new Set<AbortController>();
    private admission = new PreparationGate(5);
    private puts = new PreparationGate(2);
    private disposed = false;

    start(file: File, sha256: string, targets: UploadAssetTargets, signal: AbortSignal): Promise<PreparedUpload> {
        const started = performance.now();
        const report: UploadPreparationReport = {version: 1, elapsedMs: 0, assets: {
            preview: {target: !!targets.previewBlobUrl, generation: 'not_attempted', upload: 'not_attempted'},
            thumbnail: {target: !!targets.thumbnailBlobUrl, generation: 'not_attempted', upload: 'not_attempted'},
            metadata: {target: !!targets.metadataBlobUrl, generation: 'not_attempted', upload: 'not_attempted'},
        }};
        if (this.disposed || signal.aborted || targets.uploadPreparationEnabled === false
            || (!targets.previewBlobUrl && !targets.metadataBlobUrl && !targets.thumbnailBlobUrl)) {
            const reason = this.disposed ? 'disposed' : signal.aborted ? 'caller_aborted' : targets.uploadPreparationEnabled === false
                ? 'backend_disabled' : 'missing_target';
            report.reason = reason;
            for (const asset of PREPARATION_ASSETS) Object.assign(report.assets[asset], {generation: 'skipped', generationReason: reason});
            return Promise.resolve({preparationReport: report});
        }
        const controller = new AbortController();
        this.jobs.add(controller);
        const abort = () => { report.reason = 'caller_aborted'; controller.abort(); };
        signal.addEventListener('abort', abort, {once: true});
        let release: (() => void) | null = null;
        return this.admission.acquire(controller.signal).then(async slot => {
            release = slot;
            report.admissionMs = Math.round(performance.now() - started);
            if (!slot) return {preparationReport: report};
            return this.run(file, sha256, targets, controller.signal, report);
        }).catch(() => {
            report.reason ??= 'internal_error';
            return {preparationReport: report};
        }).finally(() => {
            if (controller.signal.aborted) report.reason ??= 'disposed';
            report.elapsedMs = Math.round(performance.now() - started);
            for (const asset of PREPARATION_ASSETS) {
                const diagnostic = report.assets[asset];
                if (diagnostic.generation === 'not_attempted') Object.assign(diagnostic, {
                    generation: diagnostic.target ? 'failed' : 'skipped',
                    generationReason: diagnostic.target ? report.reason || 'generation_failed'
                        : asset === 'metadata' ? targets.metadataPreparationReason || 'missing_target' : 'missing_target',
                });
                if (diagnostic.upload === 'aborted') diagnostic.uploadReason = report.reason || 'disposed';
            }
            release?.();
            signal.removeEventListener('abort', abort);
            this.jobs.delete(controller);
        });
    }

    dispose(): void {
        this.disposed = true;
        for (const controller of Array.from(this.jobs)) controller.abort();
        this.preparer.dispose();
    }

    private metadata(file: File, sha256: string, signal: AbortSignal, diagnostic: AssetPreparationDiagnostic): Promise<UploadMetadataPacket | null> {
        const started = performance.now();
        return new Promise(resolve => {
            let worker: Worker | undefined;
            let settled = false;
            const timer = setTimeout(() => { diagnostic.generationReason = 'generation_timeout'; finish(null); }, 30_000);
            const finish = (packet: UploadMetadataPacket | null) => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                diagnostic.generationMs = Math.round(performance.now() - started);
                diagnostic.generation = packet ? 'generated' : 'failed';
                if (!packet) diagnostic.generationReason ??= 'generation_failed';
                if (packet) diagnostic.bytes = packet.blob.size;
                worker?.terminate();
                signal.removeEventListener('abort', abort);
                resolve(packet);
            };
            const abort = () => { diagnostic.generationReason = 'aborted'; finish(null); };
            if (signal.aborted) { abort(); return; }
            try {
                worker = isVideoFilename(file.name)
                    ? new Worker(new URL('../workers/videoMetadata.worker.ts', import.meta.url), {type: 'module'})
                    : new Worker(new URL('../workers/uploadMetadata.worker.ts', import.meta.url), {type: 'module'});
                signal.addEventListener('abort', abort, {once: true});
                worker.onmessage = ({data}) => {
                    if (settled) return;
                    const packet = data?.packet;
                    if (data?.status === 'generated' && packet?.blob instanceof Blob && packet?.descriptor?.version === 1) {
                        if (isVideoFilename(file.name)) Object.assign(diagnostic, {
                            runtime: 'mediainfo_wasm', readBytes: packet.readBytes, readCalls: packet.readCalls,
                        });
                        if (packet.readBudgetReached) diagnostic.generationReason = 'metadata_read_budget';
                        finish(packet);
                    } else {
                        diagnostic.generationReason = data?.status === 'generated' ? 'packet_invalid' : data?.reason || 'generation_failed';
                        finish(null);
                    }
                };
                worker.onerror = () => { diagnostic.generationReason = 'worker_error'; finish(null); };
                worker.postMessage({file, sha256});
            } catch { diagnostic.generationReason = 'worker_start_failed'; finish(null); }
        });
    }

    private async put(url: string, blob: Blob, signal: AbortSignal, diagnostic: AssetPreparationDiagnostic, kind: PreparationAsset): Promise<boolean> {
        const queuedAt = performance.now();
        const release = await this.puts.acquire(signal, kind === 'metadata' ? 1 : 0);
        diagnostic.queueMs = Math.round(performance.now() - queuedAt);
        if (!release) { diagnostic.upload = 'aborted'; return false; }
        const started = performance.now();
        const controller = new AbortController();
        let timedOut = false;
        const abort = () => controller.abort();
        signal.addEventListener('abort', abort, {once: true});
        if (signal.aborted) abort();
        const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 45_000);
        try {
            for (let attempt = 1; attempt <= 3 && !controller.signal.aborted; attempt++) {
                diagnostic.attempts = attempt;
                let retryMs = 500 * 2 ** (attempt - 1);
                try {
                    // These PUTs (sub-1.5MB preview/thumbnail/metadata) share
                    // *.blob.core.windows.net's real ~6-connection-per-origin
                    // ceiling with the much larger, much slower original-file
                    // chunk PUTs (which can legitimately run many more than 6
                    // concurrent lanes for a big batch -- see
                    // TARGET_CONCURRENT_UPLOAD_BLOCKS's lane-budget comment in
                    // AppServicesProvider.tsx, that's deliberate, not a bug).
                    // Without a priority hint, a tiny request that would finish
                    // almost instantly can land behind a queue of multi-second
                    // chunk PUTs with no way to jump ahead -- confirmed live via
                    // HAR capture 2026-10-18: median 96% of a preview/thumbnail
                    // PUT's wall time was spent purely queued for a connection,
                    // not transferring. 'priority' (Fetch Priority API, Chrome/
                    // Edge 101+) is a hint, not a guarantee, and simply ignored
                    // by runtimes that predate it -- safe to set unconditionally.
                    const response = await fetch(url, {method: 'PUT', body: blob, signal: controller.signal,
                        priority: 'high',
                        headers: {'x-ms-blob-type': 'BlockBlob', 'Content-Type': blob.type}} as RequestInit);
                    diagnostic.httpStatus = response.status;
                    if (response.ok) { diagnostic.upload = 'uploaded'; delete diagnostic.uploadReason; return true; }
                    diagnostic.uploadReason = 'http_error';
                    if (![408, 429, 500, 502, 503, 504].includes(response.status)) break;
                    const retryAfter = response.headers?.get('Retry-After');
                    if (retryAfter) {
                        const seconds = Number(retryAfter);
                        const wait = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(retryAfter) - Date.now();
                        if (Number.isFinite(wait)) retryMs = Math.max(retryMs, wait);
                    }
                } catch {
                    delete diagnostic.httpStatus;
                    diagnostic.uploadReason = 'network_error';
                }
                if (attempt < 3 && !controller.signal.aborted) await delay(Math.min(45_000, retryMs), controller.signal);
            }
            diagnostic.upload = signal.aborted ? 'aborted' : 'failed';
            if (controller.signal.aborted) diagnostic.uploadReason = timedOut ? 'upload_timeout' : 'aborted';
            return false;
        } finally {
            clearTimeout(timer);
            signal.removeEventListener('abort', abort);
            diagnostic.uploadMs = Math.round(performance.now() - started);
            release();
        }
    }

    private async run(file: File, sha256: string, targets: UploadAssetTargets, signal: AbortSignal, report: UploadPreparationReport): Promise<PreparedUpload> {
        const assetsStarted = performance.now();
        const result: PreparedUpload = {preparationReport: report};
        const assetsPromise = targets.previewBlobUrl || (isVideoFilename(file.name) && targets.thumbnailBlobUrl)
            ? this.preparer.prepare(file, {signal}).then(async assets => {
                for (const kind of ['preview', 'thumbnail'] as const) {
                    const diagnostic = report.assets[kind];
                    if (isVideoFilename(file.name) && kind === 'preview') {
                        Object.assign(diagnostic, {generation: 'skipped', generationReason: 'not_requested'});
                        continue;
                    }
                    diagnostic.generationMs = Math.round(performance.now() - assetsStarted);
                    diagnostic.generation = assets.status === 'generated' ? 'generated' : assets.reason === 'busy' ? 'skipped' : 'failed';
                    if (assets.status === 'generated') {
                        diagnostic.bytes = assets[kind]?.blob.size; diagnostic.runtime = assets.runtime;
                        if (kind === 'preview') diagnostic.passthrough = assets.preview?.passthrough === true;
                        if (kind === 'thumbnail' && assets.readBytes !== undefined) {
                            diagnostic.readBytes = assets.readBytes; diagnostic.readCalls = assets.readCalls;
                        }
                    }
                    else diagnostic.generationReason = assets.failureReason || assets.reason || 'generation_failed';
                }
                if (assets.status === 'generated') {
                    await Promise.all((['preview', 'thumbnail'] as const).map(async kind => {
                        const url = targets[kind === 'preview' ? 'previewBlobUrl' : 'thumbnailBlobUrl'];
                        const derivative = assets[kind];
                        if (url && derivative && await this.put(url, derivative.blob, signal, report.assets[kind], kind)) {
                            result.clientProcessing ??= {};
                            result.clientProcessing[kind] = {alreadyUploaded: true, source: 'browser_upload'};
                        }
                    }));
                }
            }) : Promise.resolve(null);
        const packetPromise = targets.metadataBlobUrl ? this.metadata(file, sha256, signal, report.assets.metadata).then(async packet => {
            if (packet && await this.put(targets.metadataBlobUrl!, packet.blob, signal, report.assets.metadata, 'metadata')) {
                result.clientMetadata = packet.descriptor;
            }
        }) : Promise.resolve(null);
        const exifPromise = isVideoFilename(file.name) || signal.aborted ? Promise.resolve() : (async () => {
            const started = performance.now();
            const outcome = await file.arrayBuffer()
                .then(buffer => extractBrowserExif(new Uint8Array(buffer), file.name))
                .catch(() => ({ ok: false as const, reason: 'read_error', ms: 0 }));
            const durationMs = Math.round(performance.now() - started);
            if (outcome.ok) {
                result.clientProcessing ??= {};
                result.clientProcessing.exif = outcome.result;
                result.clientProcessingReport = [...(result.clientProcessingReport ?? []), {
                    step: 'exif', status: 'done', reason: 'done', durationMs,
                    runtime: 'browser-exiftool-wasm',
                }];
            } else {
                const unsupported = outcome.reason === 'unsupported_runtime' || outcome.reason === 'tool_unavailable';
                result.clientProcessingReport = [...(result.clientProcessingReport ?? []), {
                    step: 'exif', status: unsupported ? 'unsupported' : 'failed',
                    reason: unsupported ? 'unsupported_runtime' : 'unknown_error', durationMs,
                    runtime: 'browser-exiftool-wasm', browserExifReason: outcome.reason, browserExifMs: outcome.ms,
                }];
            }
        })();
        const outcomes = await Promise.allSettled([assetsPromise, packetPromise, exifPromise]);
        if (outcomes.some(outcome => outcome.status === 'rejected')) report.reason ??= 'internal_error';
        return result;
    }
}
