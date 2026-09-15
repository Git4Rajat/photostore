import JSZip from 'jszip';
import { resolveApiUrl } from '../services/apiClient';
import { getAccessToken, isAuthEnabled } from '../services/authClient';

type DownloadPhoto = {
    filename: string;
    url: string;
};

type DownloadProgress = {
    completed: number;
    total: number;
};

const downloadBlob = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
};

export const downloadPhotosAsZip = async (
    photos: DownloadPhoto[],
    zipName: string,
    onProgress?: (progress: DownloadProgress) => void
) => {
    const zip = new JSZip();
    let completed = 0;

    for (const photo of photos) {
        const headers: Record<string, string> = {};
        // Absolute URLs are signed storage URLs (SAS): the signature in the query
        // string IS the auth, and Azure rejects a request that carries both a SAS
        // and an Authorization header (401, "Server failed to authenticate").
        // Only backend-relative proxy paths need the bearer token.
        const isSignedStorageUrl = /^https?:\/\//i.test(photo.url);
        if (!isSignedStorageUrl && isAuthEnabled()) {
            const token = await getAccessToken();
            if (token) {
                headers.Authorization = `Bearer ${token}`;
            }
        }

        const response = await fetch(resolveApiUrl(photo.url), {
            headers,
            mode: 'cors',
            credentials: 'omit',
        });
        if (!response.ok) {
            throw new Error(`Failed to fetch ${photo.filename}`);
        }
        const blob = await response.blob();
        zip.file(photo.filename, blob);
        completed += 1;
        if (onProgress) {
            onProgress({ completed, total: photos.length });
        }
    }

    const zipBlob = await zip.generateAsync({ type: 'blob' });
    downloadBlob(zipBlob, zipName);
};
