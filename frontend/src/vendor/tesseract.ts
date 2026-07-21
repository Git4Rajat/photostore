type TesseractWorker = {
    loadLanguage?: (lang: string) => Promise<void> | void;
    initialize?: (lang: string) => Promise<void> | void;
    recognize: (source: Blob | File | string) => Promise<{ data?: { text?: string } }>;
    terminate?: () => Promise<void> | void;
};

type TesseractLike = {
    createWorker?: (...args: unknown[]) => Promise<TesseractWorker> | TesseractWorker;
};

const getGlobalTesseract = (): TesseractLike | null => {
    const globalTesseract = (globalThis as unknown as { Tesseract?: TesseractLike }).Tesseract;
    return globalTesseract || null;
};

export const createWorker = async (...args: unknown[]): Promise<TesseractWorker> => {
    const globalTesseract = getGlobalTesseract();
    if (globalTesseract?.createWorker) {
        const worker = await globalTesseract.createWorker(...args);
        return worker;
    }

    return {
        async recognize() {
            throw new Error('Tesseract worker is unavailable in this build.');
        },
        async terminate() {
            return;
        },
    };
};
