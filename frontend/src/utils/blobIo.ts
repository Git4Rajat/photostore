/**
 * Small blob/ArrayBuffer conversion helpers used by the client-side upload
 * processing pipeline. Extracted from PhotoGallery.tsx -- re-exported there
 * so AppServicesProvider's `withPhotoGalleryRuntime` lazy-import boundary
 * (`typeof import('./PhotoGallery')`) keeps resolving dataUrlToBlob/
 * readBlobArrayBuffer/sha256ArrayBuffer.
 */

export const dataUrlToBlob = async (dataUrl: string): Promise<Blob> => {
    // Decoded locally rather than via fetch(dataUrl): fetching a data: URI is
    // treated as a connect-src-governed request, which the app's CSP blocks.
    const match = dataUrl.match(/^data:([^;,]*)(;base64)?,([\s\S]*)$/);
    if (!match) {
        throw new Error('Failed to convert thumbnail data URL to blob.');
    }
    const [, mimeType, isBase64, data] = match;
    const contentType = mimeType || 'application/octet-stream';
    if (isBase64) {
        const byteString = atob(data);
        const bytes = new Uint8Array(byteString.length);
        for (let i = 0; i < byteString.length; i += 1) {
            bytes[i] = byteString.charCodeAt(i);
        }
        return new Blob([bytes], { type: contentType });
    }
    return new Blob([decodeURIComponent(data)], { type: contentType });
};

export const readBlobArrayBuffer = (blob: Blob): Promise<ArrayBuffer> => {
    if (typeof blob.arrayBuffer === 'function') {
        return blob.arrayBuffer();
    }
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            if (reader.result instanceof ArrayBuffer) {
                resolve(reader.result);
            } else {
                reject(new Error('Blob did not produce an ArrayBuffer.'));
            }
        };
        reader.onerror = () => reject(reader.error || new Error('Failed to read blob.'));
        reader.readAsArrayBuffer(blob);
    });
};

export const sha256ArrayBuffer = async (buffer: ArrayBuffer): Promise<string> => {
    const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('');
};

export const blobSha256 = async (blob: Blob): Promise<string> => sha256ArrayBuffer(await readBlobArrayBuffer(blob));
