import {extractRanges} from './metadataRanges';

export const METADATA_PACKET_MAX_BYTES = 8 * 1024 * 1024;
export interface UploadMetadataDescriptor {version: 1; bytes: number; sha256: string; sourceSha256: string;}
export interface UploadMetadataPacket {blob: Blob; descriptor: UploadMetadataDescriptor; readBytes?: number; readCalls?: number; readBudgetReached?: boolean;}

export async function createUploadMetadata(file: File, sourceSha256: string): Promise<UploadMetadataPacket> {
    if (!/^[a-f0-9]{64}$/.test(sourceSha256)) throw new Error('Invalid source hash');
    const result = await extractRanges(file, METADATA_PACKET_MAX_BYTES - 64 * 1024);
    const manifest = new TextEncoder().encode(JSON.stringify({
        version: 1, kind: result.kind, sourceSize: file.size, sourceSha256,
        ranges: result.chunks.map((chunk: {offset: number; bytes: Uint8Array}) => ({offset: chunk.offset, length: chunk.bytes.length})),
    }));
    if (manifest.length > 64 * 1024 || result.chunks.length > 4096) throw new Error('Metadata manifest limit');
    const header = new Uint8Array(4);
    new DataView(header.buffer).setUint32(0, manifest.length);
    const blob = new Blob([header, manifest, ...result.chunks.map((chunk: {bytes: Uint8Array}) => chunk.bytes)], {type: 'application/octet-stream'});
    if (blob.size > METADATA_PACKET_MAX_BYTES) throw new Error('Metadata packet limit');
    const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()));
    const sha256 = Array.from(hash, byte => byte.toString(16).padStart(2, '0')).join('');
    return {blob, descriptor: {version: 1, bytes: blob.size, sha256, sourceSha256}};
}
