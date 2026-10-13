import {UploadAssetPreparer} from './uploadAssetPreparation';
import type {UploadMetadataDescriptor, UploadMetadataPacket} from '../utils/uploadMetadata';
import {isVideoFilename} from '../utils/photoDisplay';

export interface PreparedUpload {
    clientProcessing?: Record<string, {alreadyUploaded: true; source: string}>;
    clientMetadata?: UploadMetadataDescriptor;
}
export interface UploadAssetTargets {previewBlobUrl?: string; thumbnailBlobUrl?: string; metadataBlobUrl?: string; uploadPreparationEnabled?: boolean;}

// Admission and network caps bound both retained files and derivative buffers.
export class UploadPreparationPipeline {
    private preparer = new UploadAssetPreparer();
    private jobs = new Set<AbortController>();
    private puts = 0;
    private waiting: Array<() => void> = [];
    private disposed = false;

    start(file: File, sha256: string, targets: UploadAssetTargets, signal: AbortSignal): Promise<PreparedUpload> {
        if (this.disposed || signal.aborted || isVideoFilename(file.name) || this.jobs.size >= 5
            || (!targets.previewBlobUrl && !targets.metadataBlobUrl)) return Promise.resolve({});
        const controller = new AbortController();
        this.jobs.add(controller);
        const abort = () => controller.abort();
        signal.addEventListener('abort', abort, {once: true});
        const timer = setTimeout(abort, 45_000);
        return this.run(file, sha256, targets, controller.signal).catch(() => ({})).finally(() => {
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

    private metadata(file: File, sha256: string, signal: AbortSignal): Promise<UploadMetadataPacket | null> {
        return new Promise(resolve => {
            let worker: Worker | undefined;
            let settled = false;
            const finish = (packet: UploadMetadataPacket | null) => {
                if (settled) return;
                settled = true;
                worker?.terminate();
                signal.removeEventListener('abort', abort);
                resolve(packet);
            };
            const abort = () => finish(null);
            if (signal.aborted) { resolve(null); return; }
            try {
                worker = new Worker(new URL('../workers/uploadMetadata.worker.ts', import.meta.url), {type: 'module'});
                signal.addEventListener('abort', abort, {once: true});
                worker.onmessage = ({data}) => finish(data?.status === 'generated' ? data.packet : null);
                worker.onerror = () => finish(null);
                worker.postMessage({file, sha256});
            } catch { finish(null); }
        });
    }

    private async put(url: string, blob: Blob, signal: AbortSignal): Promise<boolean> {
        if (signal.aborted) return false;
        if (this.puts >= 2) await new Promise<void>(resolve => this.waiting.push(resolve));
        else this.puts++;
        try {
            if (signal.aborted) return false;
            const response = await fetch(url, {method: 'PUT', body: blob, signal,
                headers: {'x-ms-blob-type': 'BlockBlob', 'Content-Type': blob.type}});
            return response.ok;
        } catch { return false; }
        finally {
            const next = this.waiting.shift();
            if (next) next();
            else this.puts--;
        }
    }

    private async run(file: File, sha256: string, targets: UploadAssetTargets, signal: AbortSignal): Promise<PreparedUpload> {
        const assetsPromise = targets.previewBlobUrl
            ? this.preparer.prepare(file, {signal}) : Promise.resolve(null);
        const packetPromise = targets.metadataBlobUrl ? this.metadata(file, sha256, signal) : Promise.resolve(null);
        const [assets, packet] = await Promise.all([assetsPromise, packetPromise]);
        const result: PreparedUpload = {};
        if (assets?.status === 'generated') {
            for (const kind of ['preview', 'thumbnail'] as const) {
                const url = targets[kind === 'preview' ? 'previewBlobUrl' : 'thumbnailBlobUrl'];
                if (url && await this.put(url, assets[kind].blob, signal)) {
                    result.clientProcessing ??= {};
                    result.clientProcessing[kind] = {alreadyUploaded: true, source: 'browser_upload'};
                }
            }
        }
        if (packet && targets.metadataBlobUrl && await this.put(targets.metadataBlobUrl, packet.blob, signal)) {
            result.clientMetadata = packet.descriptor;
        }
        return result;
    }
}
