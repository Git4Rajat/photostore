import { loadFaceApiRuntimeBundle } from './faceApiRuntime';

const FACE_EMBEDDING_DIMENSIONS = 640;
const FACE_EMBEDDING_VERSION = 'browser-hybrid-arcface-faceapi-v1';
const FACE_EMBEDDING_MODEL_NAME = 'ArcFace + face-api hybrid';
const FACE_EMBEDDING_MODEL_VERSION = 'arcfaceresnet100-8-512d-v1+face-recognition-128d-v1';
const FACE_EMBEDDING_RUNTIME = 'onnxruntime-web/wasm+face-api.js';
const DEFAULT_ARCFACE_MODEL_URL = '/models/browser-ai/models/arcface/model.onnx';
const DEFAULT_ARCFACE_WASM_PATH = '/models/browser-ai/runtime/';
const DEFAULT_FACE_API_MODEL_URL = '/models/face-api';

// Keep the historical exports as aliases so the rest of the app can migrate
// without churn while the embedding itself gets stronger.
export const ARCFACE_EMBEDDING_DIMENSIONS = FACE_EMBEDDING_DIMENSIONS;
export const ARCFACE_EMBEDDING_VERSION = FACE_EMBEDDING_VERSION;
export const ARCFACE_MODEL_NAME = FACE_EMBEDDING_MODEL_NAME;
export const ARCFACE_MODEL_VERSION = FACE_EMBEDDING_MODEL_VERSION;
export const ARCFACE_RUNTIME = FACE_EMBEDDING_RUNTIME;

const ARCFACE_INPUT_SIZE = 112;
const FACE_API_EMBEDDING_DIMENSIONS = 128;

type ArcFaceRuntimeOptions = {
    modelUrl?: string;
    wasmPath?: string;
    faceApiModelUrl?: string;
};

type ArcFaceSession = {
    ort: any;
    session: any;
    inputName: string;
    outputName: string;
    inputSize: number;
};

type FaceApiSession = {
    faceapi: any;
    modelUrl: string;
};

let arcFaceSessionPromise: Promise<ArcFaceSession> | null = null;
let loadedModelUrl = '';
let loadedWasmPath = '';
let faceApiSessionPromise: Promise<FaceApiSession> | null = null;
let loadedFaceApiModelUrl = '';

class ArcFaceEmbeddingUnavailableError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'ArcFaceEmbeddingUnavailableError';
        (this as any).faceFailureStage = 'embedding_model_load_failed';
        (this as any).faceFailureDetail = message;
    }
}

const normalizeModelUrl = (rawUrl?: string | null): string => {
    const trimmed = String(rawUrl || '').trim();
    if (!trimmed) {
        return DEFAULT_ARCFACE_MODEL_URL;
    }
    return trimmed.endsWith('.onnx')
        ? trimmed
        : `${trimmed.replace(/\/$/, '')}/model.onnx`;
};

const normalizeWasmPath = (rawPath?: string | null): string => {
    const trimmed = String(rawPath || '').trim();
    if (!trimmed) {
        return DEFAULT_ARCFACE_WASM_PATH;
    }
    return trimmed.endsWith('/') ? trimmed : `${trimmed}/`;
};

const normalizeFaceApiModelUrl = (rawUrl?: string | null): string => {
    const trimmed = String(rawUrl || '').trim();
    if (!trimmed) {
        return DEFAULT_FACE_API_MODEL_URL;
    }
    return trimmed.replace(/\/$/, '');
};

const loadArcFaceSession = async (options: ArcFaceRuntimeOptions = {}): Promise<ArcFaceSession> => {
    const modelUrl = normalizeModelUrl(options.modelUrl);
    const wasmPath = normalizeWasmPath(options.wasmPath);
    if (arcFaceSessionPromise && loadedModelUrl === modelUrl && loadedWasmPath === wasmPath) {
        return await arcFaceSessionPromise;
    }
    loadedModelUrl = modelUrl;
    loadedWasmPath = wasmPath;
    arcFaceSessionPromise = (async () => {
        try {
            const ort = await import('onnxruntime-web/wasm');
            if (ort.env?.wasm) {
                ort.env.wasm.wasmPaths = wasmPath;
                // Threaded WASM needs cross-origin isolation. Single-threaded is slower but more deployable.
                ort.env.wasm.numThreads = 1;
            }
            const session = await ort.InferenceSession.create(modelUrl, {
                executionProviders: ['wasm'],
                graphOptimizationLevel: 'all',
            });
            const inputName = session.inputNames?.[0];
            const outputName = session.outputNames?.[0];
            if (!inputName || !outputName) {
                throw new Error('arcface_model_missing_io_names');
            }
            return {
                ort,
                session,
                inputName,
                outputName,
                inputSize: inferInputSize(session, inputName),
            };
        } catch (err) {
            const detail = err instanceof Error ? err.message : String(err || 'arcface_model_load_failed');
            throw new ArcFaceEmbeddingUnavailableError(`arcface_model_load_failed: ${detail}`);
        }
    })().catch((err) => {
        arcFaceSessionPromise = null;
        throw err;
    });
    return await arcFaceSessionPromise;
};

const loadFaceApiSession = async (options: ArcFaceRuntimeOptions = {}): Promise<FaceApiSession> => {
    const modelUrl = normalizeFaceApiModelUrl(options.faceApiModelUrl);
    if (faceApiSessionPromise && loadedFaceApiModelUrl === modelUrl) {
        return await faceApiSessionPromise;
    }
    loadedFaceApiModelUrl = modelUrl;
    faceApiSessionPromise = (async () => {
        try {
            const faceapi = await loadFaceApiRuntimeBundle();
            if (!faceapi?.nets?.faceRecognitionNet?.loadFromUri) {
                throw new Error('faceapi_model_missing_io_names');
            }
            if (faceapi?.tf?.setBackend) {
                try {
                    await faceapi.tf.setBackend('cpu');
                } catch {
                    // Keep going; face-api can still sometimes run on the existing backend.
                }
            }
            if (faceapi?.tf?.ready) {
                await faceapi.tf.ready();
            }
            await faceapi.nets.faceRecognitionNet.loadFromUri(modelUrl);
            return { faceapi, modelUrl };
        } catch (err) {
            const detail = err instanceof Error ? err.message : String(err || 'faceapi_model_load_failed');
            throw new ArcFaceEmbeddingUnavailableError(`faceapi_model_load_failed: ${detail}`);
        }
    })().catch((err) => {
        faceApiSessionPromise = null;
        throw err;
    });
    return await faceApiSessionPromise;
};

const inferInputSize = (session: any, inputName: string): number => {
    const input = session.inputMetadata?.[inputName] || session.inputMetadata?.[0];
    const dimensions = Array.isArray(input?.dimensions) ? input.dimensions : [];
    const height = Number(dimensions[2] || 0);
    const width = Number(dimensions[3] || 0);
    if (height > 0 && width > 0 && height === width) {
        return height;
    }
    return ARCFACE_INPUT_SIZE;
};

const normalizeEmbedding = (values: ArrayLike<number>, expectedDimensions: number): Float32Array | null => {
    if (!values || values.length !== expectedDimensions) {
        return null;
    }
    let normSquared = 0;
    for (let index = 0; index < values.length; index += 1) {
        const value = Number(values[index]);
        if (!Number.isFinite(value)) {
            return null;
        }
        normSquared += value * value;
    }
    const norm = Math.sqrt(normSquared);
    if (!Number.isFinite(norm) || norm <= 0) {
        return null;
    }
    const output = new Float32Array(values.length);
    for (let index = 0; index < values.length; index += 1) {
        output[index] = Number(values[index]) / norm;
    }
    return output;
};

const canvasToNchwTensorData = (canvas: HTMLCanvasElement, inputSize: number): Float32Array => {
    const resizedCanvas = document.createElement('canvas');
    resizedCanvas.width = inputSize;
    resizedCanvas.height = inputSize;
    const context = resizedCanvas.getContext('2d', { willReadFrequently: true });
    if (!context) {
        throw new ArcFaceEmbeddingUnavailableError('arcface_canvas_context_unavailable');
    }
    context.drawImage(canvas, 0, 0, inputSize, inputSize);
    const pixels = context.getImageData(0, 0, inputSize, inputSize).data;
    const data = new Float32Array(3 * inputSize * inputSize);
    const planeSize = inputSize * inputSize;
    for (let pixelIndex = 0; pixelIndex < inputSize * inputSize; pixelIndex += 1) {
        const rgbaIndex = pixelIndex * 4;
        data[pixelIndex] = (pixels[rgbaIndex] - 127.5) / 127.5;
        data[planeSize + pixelIndex] = (pixels[rgbaIndex + 1] - 127.5) / 127.5;
        data[(planeSize * 2) + pixelIndex] = (pixels[rgbaIndex + 2] - 127.5) / 127.5;
    }
    return data;
};

const canvasToFaceApiDescriptor = async (canvas: HTMLCanvasElement, options: ArcFaceRuntimeOptions = {}) => {
    try {
        const faceApi = await loadFaceApiSession(options);
        const descriptor = await faceApi.faceapi.computeFaceDescriptor(canvas);
        return normalizeEmbedding(descriptor || [], FACE_API_EMBEDDING_DIMENSIONS);
    } catch {
        return null;
    }
};

const combineEmbeddings = (
    arcFaceEmbedding: Float32Array,
    faceApiEmbedding: Float32Array | null,
): Float32Array => {
    const combined = new Float32Array(FACE_EMBEDDING_DIMENSIONS);
    combined.set(arcFaceEmbedding, 0);
    if (faceApiEmbedding && faceApiEmbedding.length === FACE_API_EMBEDDING_DIMENSIONS) {
        combined.set(faceApiEmbedding, arcFaceEmbedding.length);
    }
    const normalized = normalizeEmbedding(combined, FACE_EMBEDDING_DIMENSIONS);
    if (!normalized) {
        throw new ArcFaceEmbeddingUnavailableError('hybrid_embedding_normalization_failed');
    }
    return normalized;
};

export const preloadArcFaceEmbeddingModel = async (options: ArcFaceRuntimeOptions = {}) => {
    await loadArcFaceSession(options);
    // Warm the second embedding path opportunistically so the first face run
    // usually only pays the inference cost, not the model load cost.
    void loadFaceApiSession(options).catch(() => undefined);
};

export const computeArcFaceEmbedding = async (
    canvas: HTMLCanvasElement,
    options: ArcFaceRuntimeOptions = {},
): Promise<Float32Array | null> => {
    const arcFace = await loadArcFaceSession(options);
    const inputSize = arcFace.inputSize || ARCFACE_INPUT_SIZE;
    const tensorData = canvasToNchwTensorData(canvas, inputSize);
    const inputTensor = new arcFace.ort.Tensor('float32', tensorData, [1, 3, inputSize, inputSize]);
    const feeds = { [arcFace.inputName]: inputTensor };
    const outputs = await arcFace.session.run(feeds);
    const outputTensor = outputs?.[arcFace.outputName] || Object.values(outputs || {})[0];
    const arcFaceEmbedding = normalizeEmbedding(outputTensor?.data || [], 512);
    if (!arcFaceEmbedding) {
        const actual = Number(outputTensor?.data?.length || 0);
        throw new ArcFaceEmbeddingUnavailableError(`arcface_embedding_dimension_mismatch: expected_512_got_${actual}`);
    }

    const faceApiEmbedding = await canvasToFaceApiDescriptor(canvas, options);
    return combineEmbeddings(arcFaceEmbedding, faceApiEmbedding);
};

export const resetArcFaceEmbeddingModelLoadStateForTests = () => {
    arcFaceSessionPromise = null;
    loadedModelUrl = '';
    loadedWasmPath = '';
    faceApiSessionPromise = null;
    loadedFaceApiModelUrl = '';
};
