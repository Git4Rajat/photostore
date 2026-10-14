import {createVideoMetadata} from '../utils/videoMetadata';
import {preparationFailureReason} from '../utils/uploadPreparationDiagnostics';

self.onmessage = async ({data}) => {
    try { self.postMessage({status: 'generated', packet: await createVideoMetadata(data.file, data.sha256)}); }
    catch (error) { self.postMessage({status: 'failed', reason: preparationFailureReason(error)}); }
};
