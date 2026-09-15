// YOLOv8n-face detector running in-browser via onnxruntime-web (same runtime as
// the ArcFace embedder). Replaces tfjs BlazeFace, which false-positived on
// patterned backgrounds. The model is a stock Ultralytics YOLOv8n-face export:
//   input  'images'  : 1x3x640x640, RGB, pixels/255
//   output 'output0' : 1x5x8400 -> [cx, cy, w, h, score] per anchor (single
//                       'face' class, NMS not baked in)
// Post-processing (letterbox -> decode -> un-letterbox -> NMS) was validated
// against onnxruntime on the real photos before this was written.

const DEFAULT_YOLO_MODEL_URL = '/models/browser-ai/models/yolo-face/model.onnx';
const DEFAULT_YOLO_WASM_PATH = '/models/browser-ai/runtime/';
const YOLO_INPUT_SIZE = 640;
// Lowered from 0.5 (2026-08-01): a real batch of 7 photos in one library all
// had a genuine, visible face scoring 0.43-0.57 -- confirmed directly via
// onnxruntime-node against the actual uploaded bytes, not guessed -- while
// every other face detected successfully in the same library scored
// 0.71-0.74. Face-area-ratio wasn't the driver (a successfully-detected face
// had a *larger* area ratio than any of the 7 failures), so this is
// per-subject/per-shot detector-confidence variance sitting right at the old
// cutoff, not a false-positive-prone zone: the original YOLOv8n-face
// evaluation (see face-detection-threshold memory) measured zero false
// positives on real curtain/foliage backgrounds down to 0.3. 0.35 keeps a
// margin below that validated-safe floor while clearing all 7 real faces.
const YOLO_SCORE_THRESHOLD = 0.35;
const YOLO_IOU_THRESHOLD = 0.45;
const YOLO_MAX_FACES = 32;

export type YoloFaceBox = {
    left: number;
    top: number;
    width: number;
    height: number;
    score: number;
};

type YoloRuntimeOptions = {
    modelUrl?: string;
    wasmPath?: string;
};

type YoloSession = {
    ort: any;
    session: any;
    inputName: string;
    outputName: string;
};

let yoloSessionPromise: Promise<YoloSession> | null = null;
let loadedModelUrl = '';
let loadedWasmPath = '';

export class YoloFaceDetectionUnavailableError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'YoloFaceDetectionUnavailableError';
        (this as any).faceFailureStage = 'model_load_failed';
        (this as any).faceFailureDetail = message;
    }
}

const normalizeModelUrl = (rawUrl?: string | null): string => {
    const trimmed = String(rawUrl || '').trim();
    if (!trimmed) {
        return DEFAULT_YOLO_MODEL_URL;
    }
    return trimmed.endsWith('.onnx') ? trimmed : `${trimmed.replace(/\/$/, '')}/model.onnx`;
};

const normalizeWasmPath = (rawPath?: string | null): string => {
    const trimmed = String(rawPath || '').trim();
    if (!trimmed) {
        return DEFAULT_YOLO_WASM_PATH;
    }
    return trimmed.endsWith('/') ? trimmed : `${trimmed}/`;
};

export const preloadYoloFaceModel = async (options: YoloRuntimeOptions = {}): Promise<YoloSession> => {
    const modelUrl = normalizeModelUrl(options.modelUrl);
    const wasmPath = normalizeWasmPath(options.wasmPath);
    if (yoloSessionPromise && loadedModelUrl === modelUrl && loadedWasmPath === wasmPath) {
        return await yoloSessionPromise;
    }
    loadedModelUrl = modelUrl;
    loadedWasmPath = wasmPath;
    yoloSessionPromise = (async () => {
        try {
            const ort = await import('onnxruntime-web/wasm');
            if (ort.env?.wasm) {
                ort.env.wasm.wasmPaths = wasmPath;
                // Threaded WASM needs cross-origin isolation; single-threaded is
                // slower but more deployable (matches the ArcFace runtime).
                ort.env.wasm.numThreads = 1;
            }
            const session = await ort.InferenceSession.create(modelUrl, {
                executionProviders: ['wasm'],
                graphOptimizationLevel: 'all',
            });
            const inputName = session.inputNames?.[0];
            const outputName = session.outputNames?.[0];
            if (!inputName || !outputName) {
                throw new Error('yolo_model_missing_io_names');
            }
            return { ort, session, inputName, outputName };
        } catch (err) {
            const detail = err instanceof Error ? err.message : String(err || 'yolo_model_load_failed');
            throw new YoloFaceDetectionUnavailableError(`yolo_model_load_failed: ${detail}`);
        }
    })().catch((err) => {
        yoloSessionPromise = null;
        throw err;
    });
    return await yoloSessionPromise;
};

type Letterbox = {
    data: Float32Array;
    scale: number;
    padX: number;
    padY: number;
};

// Resize `canvas` into a 640x640 letterbox (aspect preserved, gray padding) and
// return NCHW RGB float data normalized to [0,1], plus the transform needed to
// map detections back to `canvas` pixel coordinates.
const canvasToLetterboxTensor = (canvas: HTMLCanvasElement): Letterbox => {
    const srcW = canvas.width;
    const srcH = canvas.height;
    const scale = Math.min(YOLO_INPUT_SIZE / srcW, YOLO_INPUT_SIZE / srcH);
    const newW = Math.round(srcW * scale);
    const newH = Math.round(srcH * scale);
    const padX = Math.floor((YOLO_INPUT_SIZE - newW) / 2);
    const padY = Math.floor((YOLO_INPUT_SIZE - newH) / 2);

    const letterboxCanvas = document.createElement('canvas');
    letterboxCanvas.width = YOLO_INPUT_SIZE;
    letterboxCanvas.height = YOLO_INPUT_SIZE;
    const ctx = letterboxCanvas.getContext('2d', { willReadFrequently: true });
    if (!ctx) {
        throw new YoloFaceDetectionUnavailableError('yolo_canvas_context_unavailable');
    }
    ctx.fillStyle = 'rgb(114,114,114)';
    ctx.fillRect(0, 0, YOLO_INPUT_SIZE, YOLO_INPUT_SIZE);
    ctx.drawImage(canvas, 0, 0, srcW, srcH, padX, padY, newW, newH);

    const pixels = ctx.getImageData(0, 0, YOLO_INPUT_SIZE, YOLO_INPUT_SIZE).data;
    const plane = YOLO_INPUT_SIZE * YOLO_INPUT_SIZE;
    const data = new Float32Array(3 * plane);
    for (let i = 0; i < plane; i += 1) {
        const rgba = i * 4;
        data[i] = pixels[rgba] / 255;                 // R
        data[plane + i] = pixels[rgba + 1] / 255;     // G
        data[2 * plane + i] = pixels[rgba + 2] / 255; // B
    }
    return { data, scale, padX, padY };
};

const iou = (a: YoloFaceBox, b: YoloFaceBox): number => {
    const ax2 = a.left + a.width;
    const ay2 = a.top + a.height;
    const bx2 = b.left + b.width;
    const by2 = b.top + b.height;
    const ix1 = Math.max(a.left, b.left);
    const iy1 = Math.max(a.top, b.top);
    const ix2 = Math.min(ax2, bx2);
    const iy2 = Math.min(ay2, by2);
    const iw = Math.max(0, ix2 - ix1);
    const ih = Math.max(0, iy2 - iy1);
    const inter = iw * ih;
    const union = a.width * a.height + b.width * b.height - inter;
    return union > 0 ? inter / union : 0;
};

const nms = (boxes: YoloFaceBox[], iouThreshold: number): YoloFaceBox[] => {
    const sorted = [...boxes].sort((x, y) => y.score - x.score);
    const kept: YoloFaceBox[] = [];
    for (const box of sorted) {
        if (kept.every((k) => iou(k, box) < iouThreshold)) {
            kept.push(box);
            if (kept.length >= YOLO_MAX_FACES) {
                break;
            }
        }
    }
    return kept;
};

// Detect faces in `canvas`; returned boxes are in `canvas` pixel coordinates.
export const detectFacesWithYolo = async (
    canvas: HTMLCanvasElement,
    options: YoloRuntimeOptions = {},
): Promise<YoloFaceBox[]> => {
    const { ort, session, inputName, outputName } = await preloadYoloFaceModel(options);
    const { data, scale, padX, padY } = canvasToLetterboxTensor(canvas);
    const inputTensor = new ort.Tensor('float32', data, [1, 3, YOLO_INPUT_SIZE, YOLO_INPUT_SIZE]);
    const outputs = await session.run({ [inputName]: inputTensor });
    const output = outputs?.[outputName] || Object.values(outputs || {})[0];
    const raw = output?.data as Float32Array | undefined;
    const dims = output?.dims as number[] | undefined;
    if (!raw || !dims || dims.length !== 3) {
        return [];
    }
    // dims = [1, 5, N]: channel-major, so value(channel, anchor) = raw[channel*N + anchor].
    const channels = dims[1];
    const numAnchors = dims[2];
    if (channels < 5) {
        return [];
    }
    const cxRow = 0 * numAnchors;
    const cyRow = 1 * numAnchors;
    const wRow = 2 * numAnchors;
    const hRow = 3 * numAnchors;
    const scoreRow = 4 * numAnchors;

    const candidates: YoloFaceBox[] = [];
    for (let a = 0; a < numAnchors; a += 1) {
        const score = raw[scoreRow + a];
        if (score < YOLO_SCORE_THRESHOLD) {
            continue;
        }
        const cx = raw[cxRow + a];
        const cy = raw[cyRow + a];
        const w = raw[wRow + a];
        const h = raw[hRow + a];
        // Un-letterbox 640-space coords back to canvas pixels: undo the pad, then
        // divide by the letterbox scale (which is < 1 when the canvas was
        // downscaled to fit 640). This mirrors the validated python reference.
        const left = (cx - w / 2 - padX) / scale;
        const top = (cy - h / 2 - padY) / scale;
        const width = w / scale;
        const height = h / scale;
        const clampedLeft = Math.max(0, Math.min(left, canvas.width));
        const clampedTop = Math.max(0, Math.min(top, canvas.height));
        const clampedRight = Math.max(0, Math.min(left + width, canvas.width));
        const clampedBottom = Math.max(0, Math.min(top + height, canvas.height));
        const cw = clampedRight - clampedLeft;
        const ch = clampedBottom - clampedTop;
        if (cw <= 0 || ch <= 0) {
            continue;
        }
        candidates.push({ left: clampedLeft, top: clampedTop, width: cw, height: ch, score });
    }
    return nms(candidates, YOLO_IOU_THRESHOLD);
};

export const resetYoloFaceModelStateForTests = () => {
    yoloSessionPromise = null;
    loadedModelUrl = '';
    loadedWasmPath = '';
};
