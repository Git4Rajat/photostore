import {UploadAssetPreparer} from './uploadAssetPreparation';
import type {UploadMetadataDescriptor, UploadMetadataPacket} from '../utils/uploadMetadata';
import {isVideoFilename} from '../utils/photoDisplay';
import {PREPARATION_ASSETS, type AssetPreparationDiagnostic, type UploadPreparationReport} from '../utils/uploadPreparationDiagnostics';

export interface PreparedUpload {
    clientProcessing?: Record<string, {alreadyUploaded: true; source: string}>;
    clientMetadata?: UploadMetadataDescriptor;
    preparationReport?: UploadPreparationReport;
}
export interface UploadAssetTargets {previewBlobUrl?: string; thumbnailBlobUrl?: string; metadataBlobUrl?: string; uploadPreparationEnabled?: boolean; metadataPreparationReason?: string;}

// Admission and network caps bound both retained files and derivative buffers.
export class UploadPreparationPipeline {
    private preparer = new UploadAssetPreparer();
    private jobs = new Set<AbortController>();
    private puts = 0;
    private waiting: Array<() => void> = [];
    private disposed = false;

    start(file: File, sha256: string, targets: UploadAssetTargets, signal: AbortSignal): Promise<PreparedUpload> {
        const started = performance.now();
        const report: UploadPreparationReport = {version: 1, elapsedMs: 0, assets: {
            preview: {target: !!targets.previewBlobUrl, generation: 'not_attempted', upload: 'not_attempted'},
            thumbnail: {target: !!targets.thumbnailBlobUrl, generation: 'not_attempted', upload: 'not_attempted'},
            metadata: {target: !!targets.metadataBlobUrl, generation: 'not_attempted', upload: 'not_attempted'},
        }};
        if (this.disposed || signal.aborted || targets.uploadPreparationEnabled === false || isVideoFilename(file.name) || this.jobs.size >= 5
            || (!targets.previewBlobUrl && !targets.metadataBlobUrl)) {
            const reason = this.disposed ? 'disposed' : signal.aborted ? 'caller_aborted' : targets.uploadPreparationEnabled === false
                ? 'backend_disabled' : isVideoFilename(file.name)
                ? 'unsupported_type' : this.jobs.size >= 5 ? 'busy' : 'missing_target';
            report.reason = reason;
            for (const asset of PREPARATION_ASSETS) Object.assign(report.assets[asset], {generation: 'skipped', generationReason: reason});
            return Promise.resolve({preparationReport: report});
        }
        const controller = new AbortController();
        this.jobs.add(controller);
        const abort = () => { report.reason = 'caller_aborted'; controller.abort(); };
        signal.addEventListener('abort', abort, {once: true});
        const timer = setTimeout(() => { report.reason = 'pipeline_timeout'; controller.abort(); }, 45_000);
        return this.run(file, sha256, targets, controller.signal, report).catch(() => {
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
            clearTimeout(timer);
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
            const finish = (packet: UploadMetadataPacket | null) => {
                if (settled) return;
                settled = true;
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
                worker = new Worker(new URL('../workers/uploadMetadata.worker.ts', import.meta.url), {type: 'module'});
                signal.addEventListener('abort', abort, {once: true});
                worker.onmessage = ({data}) => {
                    const packet = data?.packet;
                    if (data?.status === 'generated' && packet?.blob instanceof Blob && packet?.descriptor?.version === 1) {
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

    private async put(url: string, blob: Blob, signal: AbortSignal, diagnostic: AssetPreparationDiagnostic): Promise<boolean> {
        const queuedAt = performance.now();
        if (signal.aborted) { diagnostic.upload = 'aborted'; return false; }
        if (this.puts >= 2) await new Promise<void>(resolve => this.waiting.push(resolve));
        else this.puts++;
        diagnostic.queueMs = Math.round(performance.now() - queuedAt);
        const started = performance.now();
        try {
            if (signal.aborted) { diagnostic.upload = 'aborted'; return false; }
            const response = await fetch(url, {method: 'PUT', body: blob, signal,
                headers: {'x-ms-blob-type': 'BlockBlob', 'Content-Type': blob.type}});
            diagnostic.httpStatus = response.status;
            diagnostic.upload = response.ok ? 'uploaded' : 'failed';
            if (!response.ok) diagnostic.uploadReason = 'http_error';
            return response.ok;
        } catch {
            diagnostic.upload = signal.aborted ? 'aborted' : 'failed';
            diagnostic.uploadReason = signal.aborted ? 'aborted' : 'network_error';
            return false;
        }
        finally {
            diagnostic.uploadMs = Math.round(performance.now() - started);
            const next = this.waiting.shift();
            if (next) next();
            else this.puts--;
        }
    }

    private async run(file: File, sha256: string, targets: UploadAssetTargets, signal: AbortSignal, report: UploadPreparationReport): Promise<PreparedUpload> {
        const assetsStarted = performance.now();
        const assetsPromise = targets.previewBlobUrl
            ? this.preparer.prepare(file, {signal}).then(assets => {
                for (const kind of ['preview', 'thumbnail'] as const) {
                    const diagnostic = report.assets[kind];
                    diagnostic.generationMs = Math.round(performance.now() - assetsStarted);
                    diagnostic.generation = assets.status === 'generated' ? 'generated' : assets.reason === 'busy' ? 'skipped' : 'failed';
                    if (assets.status === 'generated') diagnostic.bytes = assets[kind].blob.size;
                    else diagnostic.generationReason = assets.failureReason || assets.reason || 'generation_failed';
                }
                return assets;
            }) : Promise.resolve(null);
        const packetPromise = targets.metadataBlobUrl ? this.metadata(file, sha256, signal, report.assets.metadata) : Promise.resolve(null);
        const [assets, packet] = await Promise.all([assetsPromise, packetPromise]);
        const result: PreparedUpload = {preparationReport: report};
        if (assets?.status === 'generated') {
            for (const kind of ['preview', 'thumbnail'] as const) {
                const url = targets[kind === 'preview' ? 'previewBlobUrl' : 'thumbnailBlobUrl'];
                if (url && await this.put(url, assets[kind].blob, signal, report.assets[kind])) {
                    result.clientProcessing ??= {};
                    result.clientProcessing[kind] = {alreadyUploaded: true, source: 'browser_upload'};
                }
            }
        }
        if (packet && targets.metadataBlobUrl && await this.put(targets.metadataBlobUrl, packet.blob, signal, report.assets.metadata)) {
            result.clientMetadata = packet.descriptor;
        }
        return result;
    }
}
