/// <reference lib="webworker" />

import libheifModule from 'libheif-js/wasm-bundle.js';

type HeicDecodeWorkerRequest = {
    type: 'heic-decode';
    requestId: string;
    buffer: ArrayBuffer;
};

type HeifDisplayResult = { data: Uint8ClampedArray; width: number; height: number };

type HeifImage = {
    get_width(): number;
    get_height(): number;
    display(target: HeifDisplayResult, callback: (result: HeifDisplayResult | null) => void): void;
    free?: () => void;
};

type HeifDecoderInstance = { decode(buffer: Uint8Array): HeifImage[] };

const libheif = libheifModule as unknown as { HeifDecoder: new () => HeifDecoderInstance };

const decodeHeic = (buffer: ArrayBuffer): Promise<{ width: number; height: number; buffer: ArrayBuffer }> => {
    const decoder = new libheif.HeifDecoder();
    const images = decoder.decode(new Uint8Array(buffer));
    // libheif already applies the container's rotation/mirror transform (irot/imir)
    // to the decoded pixels -- unlike JPEG's EXIF Orientation, there is no separate
    // orientation step needed here (verified against real iPhone HEIC files, where
    // libheif's reported width/height already match the display-ready dimensions).
    const image = images && images[0];
    if (!image) {
        return Promise.reject(new Error('heic_no_image'));
    }
    const width = image.get_width();
    const height = image.get_height();
    if (!width || !height) {
        image.free?.();
        return Promise.reject(new Error('heic_invalid_dimensions'));
    }
    const rgba = new Uint8ClampedArray(width * height * 4);
    return new Promise((resolve, reject) => {
        image.display({ data: rgba, width, height }, (result) => {
            image.free?.();
            if (!result) {
                reject(new Error('heic_decode_failed'));
                return;
            }
            resolve({ width, height, buffer: result.data.buffer });
        });
    });
};

self.onmessage = (event: MessageEvent<HeicDecodeWorkerRequest>) => {
    const message = event.data;
    if (!message || message.type !== 'heic-decode') {
        return;
    }
    decodeHeic(message.buffer)
        .then(({ width, height, buffer }) => {
            self.postMessage({
                type: 'heic-decode-result',
                requestId: message.requestId,
                ok: true,
                width,
                height,
                buffer,
            }, [buffer]);
        })
        .catch((err) => {
            self.postMessage({
                type: 'heic-decode-result',
                requestId: message.requestId,
                ok: false,
                reason: err instanceof Error ? err.message : 'heic_decode_failed',
            });
        });
};

export {};
