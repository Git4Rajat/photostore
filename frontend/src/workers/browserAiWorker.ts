/// <reference lib="webworker" />

import { AutoModel, AutoProcessor, AutoTokenizer, env, RawImage, softmax } from '@xenova/transformers';

import type {
    BrowserAiImagePayload,
    BrowserAiManifest,
} from '../types/browserProcessing';

type BrowserAiPrediction = {
    label: string;
    score: number;
};

type BrowserAiWorkerRequest =
    | { type: 'browser-ai-warmup'; manifest: BrowserAiManifest; timeoutMs?: number }
    | { type: 'browser-ai-analyze'; requestId: string; manifest: BrowserAiManifest; image: BrowserAiImagePayload; timeoutMs?: number }

const FALLBACK_MODEL = 'photostore-local-vision-fallback';
const FALLBACK_MODEL_VERSION = 'visual-heuristics-v1';
const FALLBACK_TAXONOMY_VERSION = 'photostore-local-vision-v1';
const FALLBACK_RUNTIME = 'browser-worker/local-vision-heuristics';

// Must match the checkpoint vision_utils.py aligns its server-side CLIP text
// encoder to (openai/clip-vit-base-patch32), so photo image embeddings and
// search-query text embeddings land in the same vector space.
const IMAGE_EMBEDDING_MODEL = 'Xenova/clip-vit-base-patch32';
const IMAGE_EMBEDDING_MODEL_VERSION = 'clip-vit-base-patch32:openai:browser-v1';
const IMAGE_EMBEDDING_DIMENSION = 512;

// Open-vocabulary zero-shot tagging: instead of a fixed, narrow classifier
// (the previous ImageNet-1k classifier had ~1000 mostly product/breed-style
// classes and no generic "person", "water", "beach", or "sunset" concept at
// all), photos are tagged using CLIP's own zero-shot classification --
// comparing the image against a broader, photo-relevant vocabulary of
// candidate text labels (photostore/tools/generate_browser_ai_vocabulary.py).
// The installed @xenova/transformers version only exposes CLIP as a single
// combined image+text ONNX graph (no separate image/text encoder pipelines),
// so the vocabulary's text is tokenized and encoded once per worker session
// (cached) and reused for every photo -- only the image side changes per call.
const DEFAULT_TAG_VOCABULARY_URL = '/models/browser-ai/vocab/tag-vocabulary.v1.json';
const DEFAULT_VOCAB_TOP_K = 15;
const ZERO_SHOT_HYPOTHESIS_TEMPLATE = 'a photo of a {}';

type TagVocabulary = {
    version: string;
    labels: string[];
};

const WARMUP_IMAGE = new RawImage(
    new Uint8ClampedArray([
        255, 0, 0,
        0, 255, 0,
        0, 0, 255,
        255, 255, 255,
    ]),
    2,
    2,
    3,
);

const PERSON_LABELS = new Set([
    'person',
    'people',
    'portrait',
    'human',
    'face',
    'selfie',
    'man',
    'woman',
    'boy',
    'girl',
    'child',
    'baby',
    'infant',
    'toddler',
    'adult',
    'group',
    'family',
    'crowd',
    'bride',
    'groom',
    'bridegroom',
]);

const CLOTHING_LABEL_KEYWORDS = [
    'clothing',
    'clothes',
    'shirt',
    't-shirt',
    'tee shirt',
    'jersey',
    'sweater',
    'sweatshirt',
    'hoodie',
    'coat',
    'jacket',
    'suit',
    'tuxedo',
    'dress',
    'gown',
    'skirt',
    'pants',
    'trousers',
    'jeans',
    'shorts',
    'uniform',
    'robe',
    'kimono',
    'apron',
    'vest',
    'cardigan',
    'poncho',
    'sarong',
    'bikini',
    'swimsuit',
    'maillot',
    'brassiere',
    'bra',
    'tie',
    'scarf',
    'hat',
    'cap',
];
const CLOTHING_LABEL_PHRASES = CLOTHING_LABEL_KEYWORDS.map((keyword) => ` ${keyword.replace(/[^a-z0-9]+/g, ' ')} `);

type ClipSession = {
    model: any;
    processor: any;
    labels: string[];
    textInputs: Record<string, any>;
};

let clipSessionPromise: Promise<ClipSession> | null = null;
let clipSessionConfiguredKey = '';
let vocabularyPromise: Promise<TagVocabulary | null> | null = null;
let vocabularyConfiguredUrl = '';

const toArray = <T,>(value: unknown): T[] => (Array.isArray(value) ? value : []);

const suppressKnownTransformerWarning = (args: unknown[]) => {
    const message = toArray<unknown>(args).map((arg) => String(arg || '')).join(' ');
    return message.includes('Unable to determine content-length from response headers')
        || message.includes('Feature extractor type not specified, assuming ImageFeatureExtractor');
};

const originalConsoleWarn = console.warn.bind(console);
console.warn = (...args: unknown[]) => {
    if (suppressKnownTransformerWarning(args)) {
        return;
    }
    originalConsoleWarn(...args);
};

const postWarmupProgress = (phase: string) => {
    self.postMessage({ type: 'browser-ai-warmup-progress', phase });
};

const withWorkerTimeout = async <T,>(promise: Promise<T>, timeoutMs?: number, timeoutReason = 'inference_timeout'): Promise<T> => {
    if (!timeoutMs || timeoutMs <= 0 || !Number.isFinite(timeoutMs)) {
        return promise;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
        return await Promise.race([
            promise,
            new Promise<T>((_, reject) => {
                timer = setTimeout(() => reject(new Error(timeoutReason)), timeoutMs);
            }),
        ]);
    } finally {
        if (timer) {
            clearTimeout(timer);
        }
    }
};

const joinRuntimeAssetUrl = (basePath: string | undefined, filename: string): string => {
    const base = String(basePath || '/models/browser-ai/runtime/').trim() || '/models/browser-ai/runtime/';
    const prefix = base.endsWith('/') ? base : `${base}/`;
    return `${prefix}${filename}`;
};

const withManifestVersion = (url: string, manifest: BrowserAiManifest): string => {
    const version = String(manifest.manifestVersion || manifest.modelVersion || 'browser-ai-runtime')
        .replace(/[^A-Za-z0-9_.-]/g, '-');
    const separator = url.includes('?') ? '&' : '?';
    return `${url}${separator}v=${encodeURIComponent(version)}`;
};

const configureRuntime = (manifest: BrowserAiManifest) => {
    const runtimeEnv = env as typeof env & {
        allowLocalModels: boolean;
        allowRemoteModels: boolean;
    };
    runtimeEnv.allowLocalModels = manifest.allowLocalModels !== false;
    runtimeEnv.allowRemoteModels = manifest.allowRemoteModels !== false;
    env.localModelPath = manifest.localModelPath || '/models/browser-ai/models/';
    env.useBrowserCache = true;

    if (env.backends?.onnx?.wasm) {
        (env.backends.onnx.wasm as any).wasmPaths = {
            mjs: withManifestVersion(
                joinRuntimeAssetUrl(manifest.wasmPath, 'ort-wasm-simd-threaded.mjs'),
                manifest,
            ),
            wasm: withManifestVersion(
                joinRuntimeAssetUrl(manifest.wasmPath, 'ort-wasm-simd-threaded.wasm'),
                manifest,
            ),
        };
        env.backends.onnx.wasm.numThreads = 1;
        env.backends.onnx.wasm.simd = true;
        env.backends.onnx.wasm.proxy = false;
    }
};

const getVocabulary = async (manifest: BrowserAiManifest): Promise<TagVocabulary | null> => {
    const url = manifest.tagVocabularyUrl || DEFAULT_TAG_VOCABULARY_URL;
    if (!vocabularyPromise || vocabularyConfiguredUrl !== url) {
        vocabularyConfiguredUrl = url;
        vocabularyPromise = (async () => {
            postWarmupProgress('vocabulary_loading');
            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`vocabulary_fetch_failed:${response.status}`);
            }
            const payload = await response.json() as { version?: string; labels?: unknown };
            const labels = toArray<unknown>(payload.labels).map((label) => String(label || ''));
            if (!labels.length) {
                throw new Error('vocabulary_shape_invalid');
            }
            postWarmupProgress('vocabulary_loaded');
            return { version: String(payload.version || ''), labels };
        })().catch((err) => {
            vocabularyPromise = null;
            throw err;
        });
    }
    return vocabularyPromise;
};

// The installed @xenova/transformers version has no standalone image/text
// encoder pipeline for CLIP -- only the combined "clip" model, which requires
// both pixel_values and tokenized text in the same forward call. So the
// vocabulary's text is tokenized once here (image-independent, safe to cache
// and reuse across every photo this worker processes) and combined with each
// photo's pixel_values at inference time in a single forward pass that yields
// both the zero-shot tag logits and (if the ONNX graph exposes it) the raw
// image embedding used for search.
const getClipSession = async (manifest: BrowserAiManifest): Promise<ClipSession> => {
    const key = JSON.stringify({
        model: IMAGE_EMBEDDING_MODEL,
        localModelPath: manifest.localModelPath || '',
        allowRemoteModels: manifest.allowRemoteModels !== false,
        allowLocalModels: manifest.allowLocalModels !== false,
        vocabUrl: manifest.tagVocabularyUrl || DEFAULT_TAG_VOCABULARY_URL,
    });
    if (!clipSessionPromise || clipSessionConfiguredKey !== key) {
        clipSessionConfiguredKey = key;
        configureRuntime(manifest);
        clipSessionPromise = (async () => {
            postWarmupProgress('clip_loading');
            const vocabulary = await getVocabulary(manifest);
            if (!vocabulary) {
                throw new Error('vocabulary_unavailable');
            }
            const [model, processor, tokenizer] = await Promise.all([
                AutoModel.from_pretrained(IMAGE_EMBEDDING_MODEL, { quantized: true }),
                AutoProcessor.from_pretrained(IMAGE_EMBEDDING_MODEL),
                AutoTokenizer.from_pretrained(IMAGE_EMBEDDING_MODEL),
            ]);
            const texts = vocabulary.labels.map((label) => ZERO_SHOT_HYPOTHESIS_TEMPLATE.replace('{}', label));
            const textInputs = tokenizer(texts, { padding: true, truncation: true });
            postWarmupProgress('clip_loaded');
            return { model, processor, labels: vocabulary.labels, textInputs };
        })().catch((err) => {
            clipSessionPromise = null;
            throw err;
        });
    }
    return clipSessionPromise;
};

export const l2Normalize = (values: number[]): number[] => {
    let sumSquares = 0;
    for (const value of values) {
        sumSquares += value * value;
    }
    const norm = Math.sqrt(sumSquares);
    if (!Number.isFinite(norm) || norm <= 0) {
        return [];
    }
    return values.map((value) => value / norm);
};

// CLIP's own zero-shot classification math: softmax over logits_per_image,
// exactly what transformers.js's built-in ZeroShotImageClassificationPipeline
// does (using the same imported `softmax`) -- reusing that instead of
// hand-rolling cosine-similarity scoring keeps this aligned with CLIP's own
// learned temperature/calibration rather than an approximation of it.
export const toPredictions = (labels: string[], logits: ArrayLike<number>, topK: number): BrowserAiPrediction[] => {
    const probs = softmax(Array.from(logits));
    return labels
        .map((label, index) => ({ label, score: probs[index] }))
        .sort((a, b) => b.score - a.score)
        .slice(0, topK);
};

export const extractImageEmbedding = (rawValues: ArrayLike<number> | undefined, dimension: number): number[] => {
    if (!rawValues) {
        return [];
    }
    const values = Array.from(rawValues).map((value) => Number(value));
    if (values.length !== dimension || values.some((value) => !Number.isFinite(value))) {
        return [];
    }
    return l2Normalize(values);
};

const toRawImage = (image: BrowserAiImagePayload | RawImage): RawImage => {
    if (image instanceof RawImage) {
        return image;
    }
    const channels = image.channels === 4 ? 3 : image.channels;
    if (channels === image.channels) {
        return new RawImage(new Uint8ClampedArray(image.data), image.width, image.height, channels);
    }
    const rgbData = new Uint8ClampedArray(image.width * image.height * channels);
    for (let sourceIndex = 0, targetIndex = 0; targetIndex < rgbData.length; sourceIndex += image.channels, targetIndex += channels) {
        rgbData[targetIndex] = image.data[sourceIndex];
        rgbData[targetIndex + 1] = image.data[sourceIndex + 1];
        rgbData[targetIndex + 2] = image.data[sourceIndex + 2];
    }
    return new RawImage(rgbData, image.width, image.height, channels);
};

const labelSuggestsClothingColorContext = (label: string): boolean => {
    if (PERSON_LABELS.has(label)) {
        return true;
    }
    const words = ` ${label.replace(/[^a-z0-9]+/g, ' ')} `;
    return CLOTHING_LABEL_PHRASES.some((phrase) => words.includes(phrase));
};

const mergeTags = (...groups: string[][]): string[] => {
    const tags: string[] = [];
    const seen = new Set<string>();
    for (const group of groups) {
        for (const tag of group) {
            const normalized = String(tag || '').trim().toLowerCase();
            if (!normalized || seen.has(normalized)) {
                continue;
            }
            seen.add(normalized);
            tags.push(normalized);
        }
    }
    return tags;
};

const rgbToColorName = (red: number, green: number, blue: number): string | null => {
    const r = red / 255;
    const g = green / 255;
    const b = blue / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    const delta = max - min;
    const saturation = max === 0 ? 0 : delta / max;
    const value = max;

    if (value < 0.18) {
        return 'black';
    }
    if (saturation < 0.12) {
        return value > 0.82 ? 'white' : 'gray';
    }

    let hue = 0;
    if (delta !== 0) {
        if (max === r) {
            hue = 60 * (((g - b) / delta) % 6);
        } else if (max === g) {
            hue = 60 * (((b - r) / delta) + 2);
        } else {
            hue = 60 * (((r - g) / delta) + 4);
        }
    }
    if (hue < 0) {
        hue += 360;
    }

    if (hue >= 18 && hue < 48 && value < 0.62 && saturation > 0.32) {
        return 'brown';
    }
    if (hue >= 18 && hue < 45) {
        return 'orange';
    }
    if (hue >= 45 && hue < 70) {
        return 'yellow';
    }
    if (hue >= 70 && hue < 165) {
        return 'green';
    }
    if (hue >= 165 && hue < 250) {
        return 'blue';
    }
    if (hue >= 250 && hue < 292) {
        return 'purple';
    }
    if (hue >= 292 && hue < 340) {
        return 'pink';
    }
    return 'red';
};

const inferClothingColorTags = (image: BrowserAiImagePayload, accepted: BrowserAiPrediction[]): string[] => {
    if (!accepted.some((item) => labelSuggestsClothingColorContext(item.label))) {
        return [];
    }

    const channels = image.channels;
    const left = Math.floor(image.width * 0.22);
    const right = Math.ceil(image.width * 0.78);
    const top = Math.floor(image.height * 0.36);
    const bottom = Math.ceil(image.height * 0.92);
    const sampleWidth = Math.max(0, right - left);
    const sampleHeight = Math.max(0, bottom - top);
    if (!sampleWidth || !sampleHeight || !image.data?.length) {
        return [];
    }

    const stride = Math.max(1, Math.floor(Math.sqrt((sampleWidth * sampleHeight) / 12000)));
    const counts = new Map<string, number>();
    let totalWeight = 0;
    for (let y = top; y < bottom; y += stride) {
        const rowWeight = 0.75 + ((y - top) / Math.max(1, sampleHeight)) * 0.5;
        for (let x = left; x < right; x += stride) {
            const index = ((y * image.width) + x) * channels;
            const alpha = channels === 4 ? Number(image.data[index + 3] || 0) : 255;
            if (alpha < 32) {
                continue;
            }
            const color = rgbToColorName(
                Number(image.data[index] || 0),
                Number(image.data[index + 1] || 0),
                Number(image.data[index + 2] || 0),
            );
            if (!color) {
                continue;
            }
            counts.set(color, (counts.get(color) || 0) + rowWeight);
            totalWeight += rowWeight;
        }
    }

    if (totalWeight < 40) {
        return [];
    }
    const dominant = Array.from(counts.entries()).sort((a, b) => b[1] - a[1])[0];
    if (!dominant || dominant[1] / totalWeight < 0.18) {
        return [];
    }
    const color = dominant[0];
    return color === 'gray'
        ? ['clothing', 'gray clothing', 'gray', 'grey clothing', 'grey']
        : ['clothing', `${color} clothing`, color];
};

const analyzeImageLocally = (_manifest: BrowserAiManifest, _image: BrowserAiImagePayload | RawImage) => {
    return {
        predictions: [],
        tags: [],
        objects: [],
        caption: '',
        ocrText: '',
        aiPersonCandidate: false,
        aiPersonLabel: '',
        aiPersonScore: 0,
        model: FALLBACK_MODEL,
        modelVersion: FALLBACK_MODEL_VERSION,
        modelTaxonomyVersion: FALLBACK_TAXONOMY_VERSION,
        runtime: FALLBACK_RUNTIME,
        fallbackReason: 'classifier_unavailable',
    };
};

const firstRow = (tensor: any): ArrayLike<number> | undefined => {
    if (!tensor) {
        return undefined;
    }
    for (const row of tensor) {
        return row?.data;
    }
    return undefined;
};

const classify = async (manifest: BrowserAiManifest, image: BrowserAiImagePayload | RawImage): Promise<{
    predictions: BrowserAiPrediction[];
    imageEmbedding: number[];
}> => {
    postWarmupProgress('warmup_inference');
    const session = await getClipSession(manifest);
    const { pixel_values: pixelValues } = await session.processor([toRawImage(image)]);
    const output = await session.model({ ...session.textInputs, pixel_values: pixelValues });

    const logits = firstRow(output?.logits_per_image);
    if (!logits) {
        throw new Error('clip_forward_failed');
    }
    const topK = Math.max(1, Math.min(Number(manifest.vocabTopK || manifest.topK || DEFAULT_VOCAB_TOP_K), session.labels.length));
    const predictions = toPredictions(session.labels, logits, topK);
    const imageEmbedding = extractImageEmbedding(firstRow(output?.image_embeds), IMAGE_EMBEDDING_DIMENSION);
    return { predictions, imageEmbedding };
};

const analyze = async (manifest: BrowserAiManifest, image: BrowserAiImagePayload) => {
    let predictions: BrowserAiPrediction[] = [];
    let imageEmbedding: number[] = [];
    try {
        const result = await classify(manifest, image);
        predictions = result.predictions;
        imageEmbedding = result.imageEmbedding;
    } catch (err) {
        if (manifest.enableLocalVisionFallback !== false) {
            return analyzeImageLocally(manifest, image);
        }
        throw err;
    }
    const topK = Math.max(1, Math.min(Number(manifest.vocabTopK || manifest.topK || DEFAULT_VOCAB_TOP_K), predictions.length));
    // Send the full top-K candidate set with each label's real score attached
    // (via the `predictions` field, propagated to storage_utils' confidence
    // passthrough) rather than pre-filtering by score here -- confidence-based
    // filtering belongs server-side in curate_tag_records, which has the real
    // score to make that call per tag instead of a flat per-source default.
    const accepted = predictions.slice(0, topK);
    const bestPerson = predictions.find((item) => PERSON_LABELS.has(item.label));
    const personScoreThreshold = Number(manifest.personScoreThreshold || 0.2);
    const tags = mergeTags(
        accepted.map((item) => item.label),
        inferClothingColorTags(image, accepted),
    );
    return {
        predictions,
        tags,
        objects: tags,
        caption: '',
        ocrText: '',
        aiPersonCandidate: Boolean(bestPerson && bestPerson.score >= personScoreThreshold),
        aiPersonLabel: bestPerson?.label || '',
        aiPersonScore: bestPerson ? Number(bestPerson.score.toFixed(4)) : 0,
        imageEmbedding,
        imageEmbeddingModel: imageEmbedding.length ? IMAGE_EMBEDDING_MODEL_VERSION : '',
    };
};

const warmup = async (manifest: BrowserAiManifest) => {
    try {
        await classify(manifest, WARMUP_IMAGE);
        return { fallback: false };
    } catch (err) {
        if (manifest.enableLocalVisionFallback !== false) {
            analyzeImageLocally(manifest, WARMUP_IMAGE);
            return {
                fallback: true,
                reason: err instanceof Error ? err.message : 'classifier_unavailable',
            };
        }
        throw err;
    }
};

self.onmessage = (event: MessageEvent<BrowserAiWorkerRequest>) => {
    const message = event.data;
    if (!message || !message.type) {
        return;
    }
    if (message.type === 'browser-ai-warmup') {
        postWarmupProgress('worker_received');
        void withWorkerTimeout(warmup(message.manifest || {}), message.timeoutMs, 'model_download_timeout')
            .then((result) => {
                self.postMessage({
                    type: 'browser-ai-warmup-result',
                    ok: true,
                    fallback: result.fallback,
                    reason: result.reason,
                    model: result.fallback ? FALLBACK_MODEL : undefined,
                    modelVersion: result.fallback ? FALLBACK_MODEL_VERSION : undefined,
                    modelTaxonomyVersion: result.fallback ? FALLBACK_TAXONOMY_VERSION : undefined,
                    runtime: result.fallback ? FALLBACK_RUNTIME : undefined,
                });
            })
            .catch((err) => {
                self.postMessage({
                    type: 'browser-ai-warmup-result',
                    ok: false,
                    reason: err instanceof Error ? err.message : 'model_load_failed',
                });
            });
        return;
    }
    if (message.type === 'browser-ai-analyze') {
        void withWorkerTimeout(analyze(message.manifest || {}, message.image), message.timeoutMs)
            .then((result) => {
                self.postMessage({
                    type: 'browser-ai-analyze-result',
                    requestId: message.requestId,
                    ok: true,
                    result,
                });
            })
            .catch((err) => {
                self.postMessage({
                    type: 'browser-ai-analyze-result',
                    requestId: message.requestId,
                    ok: false,
                    reason: err instanceof Error ? err.message : 'model_load_failed',
                });
            });
        return;
    }
};

export {};
