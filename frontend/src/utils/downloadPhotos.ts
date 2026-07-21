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
        if (isAuthEnabled()) {
            const token = await getAccessToken();
            if (token) {
                headers.Authorization = `Bearer ${token}`;
            }
        }

        const response = await fetch(resolveApiUrl(photo.url), {
            headers,
            mode: 'cors',
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
