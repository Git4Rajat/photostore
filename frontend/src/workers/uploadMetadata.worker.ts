import {createUploadMetadata} from '../utils/uploadMetadata';

self.onmessage = async ({data}) => {
    try {
        self.postMessage({status: 'generated', packet: await createUploadMetadata(data.file, data.sha256)});
    } catch {
        self.postMessage({status: 'fallback'});
    }
};
