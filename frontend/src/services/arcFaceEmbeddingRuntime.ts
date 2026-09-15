import { loadFaceApiRuntimeBundle } from './faceApiRuntime';

const FACE_EMBEDDING_DIMENSIONS = 512;
// v3: drop the 128-d face-api descriptor that was concatenated onto ArcFace's
// 512-d output (it diluted ArcFace's own signal) and feed ArcFace a
// landmark-aligned crop instead of a plain padded box. face-api.js is still
// used, but now for its 68-point landmark net (5-point alignment), not for a
// descriptor.
// -guarded: same v3 model/alignment pipeline, but cropFaceCanvas now rejects a
// geometrically-implausible 5-point solve (bad landmarks on a hard-angle face
// producing a garbage transform) and falls back instead of trusting it, and
// each face is tagged with which alignment tier it actually got. Bumped
// purely to force the self-healing re-embed across the library so existing
// faces pick up the guard + get tagged, not because the model itself changed.
// -diag: same pipeline again; detectFaceLandmarks no longer silently swallows
// its own errors (a bare catch{return null} was hiding the real cause behind
// alignmentMethod='none' landing on 100% of faces in real testing) and
// detectFiveFaceLandmarks now records *why* into alignmentFailureReason.
// -fixed: the -diag deploy caught the real cause via alignmentFailureReason:
// "faceapi_model_load_failed: No backend found in registry." loadFaceApiSession
// called tf.setBackend('cpu') without ever importing '@tensorflow/tfjs-backend-cpu'
// first (tfjs backends only self-register via that import's side effect) — it was
// silently relying on loadTensorFlowRuntime (PhotoGallery.tsx, a wholly separate
// initialization path for the browser-AI tagging feature) to have already done
// this, which isn't guaranteed. Added the missing import directly here.
// -fixed2: -fixed got past the backend error but hit a new one one layer
// deeper, again via alignmentFailureReason: "e.toFloat is not a function".
// face-api.js's bundled code (built against tfjs-core@1.7.0) calls legacy
// convenience cast methods (Tensor.prototype.toFloat/toInt/toBool) that were
// removed from tfjs-core in later versions -- confirmed the app's deduped
// tfjs-core@4.22.0 only has .cast(dtype) now.
// -fixed3: -fixed2's shim only covered toFloat/toInt/toBool; the very next
// call in the same chain (dom/NetInput.js's toBatchTensor: `...toFloat().as4D(...)`)
// hit "e.as4D is not a function". Checked directly: tfjs-core@4.22.0 removed
// essentially ALL chainable Tensor op methods (add/sub/reshape/expandDims/
// stack/... -- 257 of them), not just casts, in favor of top-level functional
// calls (tf.add(t,...), tf.reshape(t,...)). faceApiRuntime.ts now generically
// restores every tf.<op> as an instance method forwarding to the top-level
// call, plus explicit mappings for the handful with no same-named top-level
// equivalent (toFloat/toInt/toBool -> .cast, as1D..as5D -> tf.reshape,
// resizeBilinear -> tf.image.resizeBilinear). Verified end-to-end against a
// real production face crop via an esbuild-bundled repro of the actual
// source before shipping -fixed3 -- but that repro used the EXACT failing
// crop and still passed, while the real deploy hit a 3rd error:
// "Size(136) must match the product of shape" (the shape stringifying to ''
// because it was [] -- an empty array). Root cause, confirmed by isolating
// the exact chain face-api's postProcess uses (`...as2D(1,136).as1D()`):
// as1D() is called with ZERO arguments in legacy usage (means "flatten to
// 1D, infer the size"), unlike as2D..as5D which always take explicit dims.
// The generic shim forwarded the (empty) args array straight to
// tf.reshape(this, []), targeting a 0-rank scalar instead. Fixed by
// special-casing as1D to reshape to [this.size].
// -fixed5: -fixed4 finally got real landmarks end-to-end (no more crashes),
// but 24/25 faces landed on alignmentMethod='landmark-2pt', not the
// intended 'landmark-5pt'. Root cause (PhotoGallery.tsx's
// ARC_FACE_MIN_SCALE/MAX_SCALE): the alignment-quality guard's bounds
// (0.65-3.5) were copied from the old 2-point path's *different*
// target-eye-distance convention -- for the 5-point path's actual crop
// convention (1.25x-padded full face bbox, not a tight eye-only crop), a
// geometrically CORRECT solve legitimately scores 0.08-0.25, so the guard
// was rejecting essentially every real transform as a false positive.
// Confirmed directly: isolated 6 real production faces that fell back to
// 2pt, computed their actual solved scale/rotation, and 5/6 were genuinely
// valid (sane rotation, scale just outside the miscalibrated bounds) --
// recalibrated to 0.03-0.6 and reverified the same 6 faces now pass (the
// 6th, with 61.8 degree rotation, still correctly rejects).
// -fixed6: real cross-photo testing on the recalibrated guard showed the
// 2-point eye-only fallback (PhotoGallery.tsx cropFaceCanvas) actively hurts
// matches -- the same confirmed person, one photo 5-point-aligned and
// another only 2-point-aligned, scored near-zero similarity purely from the
// alignment-tier mismatch, not a real identity difference. Removed the
// 2-point fallback entirely: a face either gets real 5-point alignment or
// falls straight to a plain unaligned crop (still stored/visible, but
// excluded from clustering by the backend's alignmentMethod gate). One
// embedding quality tier in the matching pool, not several mixed together.
// -fixed7: -fixed6's alignment guard (ARC_FACE_MAX_SCALE=0.6) was too tight
// -- a confirmed-real, correctly-detected face (downward head tilt, which
// foreshortens apparent eye distance in 2D and pushes solved scale up)
// measured 0.68 and was wrongly rejected to 'none'. Raised MAX_SCALE to 0.9.
// -fixed8: -fixed7 was reverted. Real ArcFace embedding testing (actual
// model inference via onnxruntime-node locally, not just checking the
// transform's numbers look plausible) proved 5-point alignment for that
// same downward-tilt case scored only 0.19-0.28 same-person similarity --
// WORSE than a plain unaligned crop (0.56-0.68) or the 2-point eyes-only
// fallback (0.51-0.55). Forcing a severely pitched face into a frontal
// template distorts it more than leaving it alone. MAX_SCALE reverted to
// 0.6 (PhotoGallery.tsx), and the 2-point fallback (PhotoGallery.tsx
// cropFaceCanvas) is restored as a genuinely separate, real quality tier --
// not a last resort. Backend now clusters landmark-2pt faces in their own
// DBSCAN pass with a separately-calibrated epsilon
// (PEOPLE_CLUSTER_EPS_2PT=0.60), and never compares a 2pt embedding
// directly against a 5pt one (confirmed cross-tier same-person comparisons
// are unreliable: 0.09-0.56, indistinguishable from noise).
// -adaface1: swapped the embedding backbone itself (ArcFace resnet100 ->
// AdaFace IR-101, WebFace4M). Even -fixed8's tier-aware clustering can't fix
// a case where BOTH tiers score low because the pose gap is real, not an
// alignment artifact: a confirmed same-person pair at genuinely different
// head-turn/tilt scored only 0.29 under ArcFace, on the best (5-point)
// tier. Measured head-to-head on identical crops (same onnxruntime-node
// harness used for -fixed8): AdaFace scored 0.68 on that same pair (vs
// ArcFace's 0.31 on the same unaligned crop), 0.82-0.85 on a moderate-pose
// burst-shot trio (vs ArcFace's 0.56-0.67), while a genuinely easy/frontal
// pair stayed near ceiling for both (0.89 vs 0.86) -- the gain is
// concentrated in hard-pose cases, not a general shift, consistent with
// AdaFace's quality-adaptive training margin. AdaFace is not immune to a
// badly-distorted 5-point alignment though (confirmed: the same warped
// crop that broke ArcFace also degrades AdaFace to 0.15-0.58) -- it still
// needs a reasonably sane crop, so the existing alignment/tier pipeline is
// unchanged here. Weights: official AdaFace authors' checkpoint
// (huggingface.co/minchul/cvlface_adaface_ir101_webface4m, MIT), IR-100
// backbone reverse-engineered from the safetensors tensor names/shapes (no
// code from that repo is used) and exported to ONNX with the same
// (x/127.5-1) normalization baked into the graph as the old ArcFace model,
// so the raw-pixel calling convention below is unchanged. Verified
// bit-identical output across PyTorch -> ONNX(python) -> ONNX(node) before
// shipping.
// -adaface1-fixed: -adaface1's rollout never actually took effect. The model
// URL used at inference time (PhotoGallery.tsx's collectBlazeFaceCandidates/
// detectFacesWithEmbeddings) doesn't come from DEFAULT_ARCFACE_MODEL_URL
// above -- it comes from window.__APP_CONFIG__.arcFaceModelUrl, injected at
// container start by docker-entrypoint.sh, which falls back to the OLD
// arcface path unless APP_CONFIG_ARC_FACE_MODEL_URL is explicitly set. It
// wasn't (deploy/resources.bicep had no pin for it), so the browser kept
// loading the old ArcFace model the entire time -- confirmed directly:
// faces re-embedded and relabeled to -adaface1 post-deploy had cosine
// similarities bit-identical (to 4 decimals, across 4 independent pairs) to
// their pre-swap ArcFace values. The version *label* changed (a compile-time
// constant, unaffected by the runtime config bug); the actual embedding
// computed did not. Fixed by pinning APP_CONFIG_ARC_FACE_MODEL_URL in
// resources.bicep and correcting the hardcoded fallback defaults in
// Dockerfile/docker-entrypoint.sh. Re-bumping the version string (rather than
// just fixing the config) because every face already carries the -adaface1
// label -- the staleness check (stored version != current version) would
// otherwise see nothing to re-embed and this bug would go undetected forever.
const FACE_EMBEDDING_VERSION = 'browser-adaface-ir101-v1-fixed';
const FACE_EMBEDDING_MODEL_NAME = 'AdaFace IR-101 (5-point aligned)';
const FACE_EMBEDDING_MODEL_VERSION = 'adaface-ir101-webface4m-512d-v1+face-landmark-68-v1';
const FACE_EMBEDDING_RUNTIME = 'onnxruntime-web/wasm+face-api.js';
const DEFAULT_ARCFACE_MODEL_URL = '/models/browser-ai/models/adaface/model.onnx';
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

export type FaceLandmarkPoint = { x: number; y: number };

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
            if (!faceapi?.nets?.faceLandmark68Net?.loadFromUri) {
                throw new Error('faceapi_model_missing_io_names');
            }
            if (faceapi?.tf?.setBackend) {
                // tfjs backends only register themselves via this import's side
                // effect; without it, setBackend('cpu') always fails with "No
                // backend found in registry" (confirmed via alignmentFailureReason
                // in real testing — every face fell back to alignmentMethod='none'
                // because of this). loadTensorFlowRuntime (PhotoGallery.tsx) already
                // does this for the unrelated browser-AI tagging feature, but that's
                // a separate initialization path this one can't rely on running first.
                try {
                    await import('@tensorflow/tfjs-backend-cpu');
                } catch {
                    // Best effort; setBackend below will surface a concrete failure.
                }
                try {
                    await faceapi.tf.setBackend('cpu');
                } catch {
                    // Keep going; face-api can still sometimes run on the existing backend.
                }
            }
            if (faceapi?.tf?.ready) {
                await faceapi.tf.ready();
            }
            await faceapi.nets.faceLandmark68Net.loadFromUri(modelUrl);
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

type NormalizeEmbeddingResult = { embedding: Float32Array | null; reason?: string };

// A face silently vanishing from a photo with faces detected (2026-08-01: ~4/24
// photos in one batch) traced back to this function passing a bad result
// through undetected: it validated the RAW output values and the norm are
// finite, but never checked the result of dividing by that norm. A norm small
// enough to be finite and >0 can still push value/norm past Float32 range,
// producing Infinity that this function returned as if valid -- silently
// caught one layer up instead (PhotoGallery.tsx's redundant second
// normalizeFaceEmbedding check), where there was no path to record why. Now
// checks the post-division result too, and reports a specific reason instead
// of returning bare null, so a real failure throws (visible, retriable) with
// an accurate cause instead of vanishing.
const normalizeEmbedding = (values: ArrayLike<number>, expectedDimensions: number): NormalizeEmbeddingResult => {
    if (!values || values.length !== expectedDimensions) {
        return { embedding: null, reason: `dimension_mismatch: expected_${expectedDimensions}_got_${values ? values.length : 0}` };
    }
    let normSquared = 0;
    let nonFiniteRawCount = 0;
    for (let index = 0; index < values.length; index += 1) {
        const value = Number(values[index]);
        if (!Number.isFinite(value)) {
            nonFiniteRawCount += 1;
            continue;
        }
        normSquared += value * value;
    }
    if (nonFiniteRawCount > 0) {
        return { embedding: null, reason: `non_finite_raw_values: ${nonFiniteRawCount}_of_${values.length}` };
    }
    const norm = Math.sqrt(normSquared);
    if (!Number.isFinite(norm) || norm <= 0) {
        return { embedding: null, reason: `invalid_norm: ${norm}` };
    }
    const output = new Float32Array(values.length);
    let nonFiniteOutputCount = 0;
    for (let index = 0; index < values.length; index += 1) {
        const divided = Number(values[index]) / norm;
        if (!Number.isFinite(divided)) {
            nonFiniteOutputCount += 1;
        }
        output[index] = divided;
    }
    if (nonFiniteOutputCount > 0) {
        return { embedding: null, reason: `non_finite_after_normalize: ${nonFiniteOutputCount}_of_${values.length}_norm_${norm}` };
    }
    return { embedding: output };
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
    // This ArcFace ONNX graph bakes its own preprocessing in as its first two
    // ops (`_minusscalar0` subtracts 127.5, `_mulscalar0` multiplies by
    // 1/128), so it expects RAW [0,255] RGB pixels. Do NOT normalize here — a
    // second (pixel-127.5)/127.5 pass crushes every pixel into a ~0.016-wide
    // band near -1, wiping out the face and collapsing all embeddings so that
    // unrelated people score ~0.98 cosine against each other.
    for (let pixelIndex = 0; pixelIndex < inputSize * inputSize; pixelIndex += 1) {
        const rgbaIndex = pixelIndex * 4;
        data[pixelIndex] = pixels[rgbaIndex];
        data[planeSize + pixelIndex] = pixels[rgbaIndex + 1];
        data[(planeSize * 2) + pixelIndex] = pixels[rgbaIndex + 2];
    }
    return data;
};

export const preloadArcFaceEmbeddingModel = async (options: ArcFaceRuntimeOptions = {}) => {
    await loadArcFaceSession(options);
    // Warm the landmark path opportunistically so the first face run usually
    // only pays the inference cost, not the model load cost.
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
    const { embedding, reason } = normalizeEmbedding(outputTensor?.data || [], FACE_EMBEDDING_DIMENSIONS);
    if (!embedding) {
        throw new ArcFaceEmbeddingUnavailableError(`arcface_embedding_invalid: ${reason || 'unknown'}`);
    }
    return embedding;
};

// Runs face-api's 68-point landmark net on `canvas` (expected to already
// contain a single, roughly-centered face — same calling convention as the
// old descriptor path this replaces) and returns the raw points, or null if
// the model ran but didn't find enough points. Deliberately does NOT catch
// here — every real-world run so far has produced alignmentMethod='none' for
// 100% of faces with zero visibility into why, because a previous version of
// this function swallowed the error. Let it throw; the caller
// (detectFiveFaceLandmarks in PhotoGallery.tsx) is responsible for catching
// and recording the actual failure reason so it's visible in stored face data
// instead of silently lost.
export const detectFaceLandmarks = async (
    canvas: HTMLCanvasElement,
    options: ArcFaceRuntimeOptions = {},
): Promise<FaceLandmarkPoint[] | null> => {
    const faceApi = await loadFaceApiSession(options);
    const result = await faceApi.faceapi.detectFaceLandmarks(canvas);
    const landmarks = Array.isArray(result) ? result[0] : result;
    const positions = landmarks?.positions;
    if (!Array.isArray(positions) || positions.length < 68) {
        return null;
    }
    return positions.map((point: any) => ({ x: Number(point?.x), y: Number(point?.y) }));
};

export const resetArcFaceEmbeddingModelLoadStateForTests = () => {
    arcFaceSessionPromise = null;
    loadedModelUrl = '';
    loadedWasmPath = '';
    faceApiSessionPromise = null;
    loadedFaceApiModelUrl = '';
};
