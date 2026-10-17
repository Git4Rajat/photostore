import {createUploadMetadata} from '../utils/uploadMetadata';
import {preparationFailureReason} from '../utils/uploadPreparationDiagnostics';

self.onmessage = async ({data}) => {
    try {
        self.postMessage({status: 'generated', packet: await createUploadMetadata(data.file, data.sha256)});
    } catch (error) {
        self.postMessage({status: 'fallback', reason: preparationFailureReason(error)});
    }
};
