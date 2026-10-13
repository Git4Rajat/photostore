import {Blob as NodeBlob} from 'node:buffer';
import {webcrypto} from 'node:crypto';
import {createUploadMetadata} from './uploadMetadata';

beforeEach(() => { vi.stubGlobal('Blob', NodeBlob); vi.stubGlobal('crypto', webcrypto); });
afterEach(() => vi.unstubAllGlobals());

const file = (bytes: Uint8Array) => Object.assign(new NodeBlob([bytes]), {name: 'test.dng'}) as unknown as File;
function tiff() {
    const bytes = new Uint8Array(512);
    const view = new DataView(bytes.buffer);
    bytes.set([73, 73, 42, 0, 8, 0, 0, 0]);
    view.setUint16(8, 1, true);
    view.setUint16(10, 0x010f, true); // Make, out-of-line ASCII.
    view.setUint16(12, 2, true);
    view.setUint32(14, 6, true);
    view.setUint32(18, 400, true);
    bytes.set(new TextEncoder().encode('Nikon\0'), 400);
    return bytes;
}

test('copies out-of-line bytes unchanged and binds packet to original hash', async () => {
    const bytes = tiff();
    const result = await createUploadMetadata(file(bytes), 'a'.repeat(64));
    const packet = new Uint8Array(await result.blob.arrayBuffer());
    const length = new DataView(packet.buffer).getUint32(0);
    const manifest = JSON.parse(new TextDecoder().decode(packet.subarray(4, 4 + length)));
    expect(manifest.sourceSize).toBe(512);
    expect(manifest.sourceSha256).toBe('a'.repeat(64));
    expect(manifest.ranges).toEqual([{offset: 0, length: 256}, {offset: 400, length: 6}]);
    expect(Array.from(packet.slice(-6))).toEqual(Array.from(bytes.slice(400, 406)));
    expect(result.descriptor).toMatchObject({version: 1, bytes: packet.length, sourceSha256: 'a'.repeat(64)});
    const digest = await webcrypto.subtle.digest('SHA-256', packet);
    expect(result.descriptor.sha256).toBe(Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join(''));
});

test('partial traversal and unknown types cannot be uploaded as complete metadata', async () => {
    const bytes = tiff();
    new DataView(bytes.buffer).setUint32(18, 600, true);
    await expect(createUploadMetadata(file(bytes), 'a'.repeat(64))).rejects.toThrow('Incomplete');
    new DataView(bytes.buffer).setUint16(12, 99, true);
    await expect(createUploadMetadata(file(bytes), 'a'.repeat(64))).rejects.toThrow('Unsupported TIFF');
});

test('unsupported containers and malformed hashes fall back', async () => {
    await expect(createUploadMetadata(file(new Uint8Array([137, 80, 78, 71])), 'a'.repeat(64))).rejects.toThrow('Unsupported');
    await expect(createUploadMetadata(file(tiff()), 'bad')).rejects.toThrow('Invalid source hash');
});

test('JPEG header transport stops before sensor/scan bytes', async () => {
    const bytes = new Uint8Array(2000);
    bytes.set([255, 216, 255, 225, 0, 4, 69, 88, 255, 218, 0, 2]);
    const result = await createUploadMetadata(file(bytes), 'a'.repeat(64));
    const packet = new Uint8Array(await result.blob.arrayBuffer());
    const length = new DataView(packet.buffer).getUint32(0);
    const manifest = JSON.parse(new TextDecoder().decode(packet.subarray(4, 4 + length)));
    expect(manifest.kind).toBe('jpeg-header-ranges');
    expect(manifest.ranges).toEqual([{offset: 0, length: 256}]);
    expect(packet.length - 4 - length).toBe(256);
});
