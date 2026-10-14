/// <reference lib="webworker" />

import {decodeHeic} from '../utils/decodeHeic';

type HeicDecodeWorkerRequest = {
    type: 'heic-decode';
    requestId: string;
    buffer: ArrayBuffer;
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
