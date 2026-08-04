// face-api.js@0.22.2 was built against tfjs-core@1.7.0's fluent/chainable
// Tensor API (tensor.add(x), tensor.reshape(shape), tensor.toFloat(), ...).
// This app's tfjs-core is deduped to a much newer 4.22.0 (shared with the
// unrelated browser-AI tagging feature, via the alias in vite.config.ts),
// where nearly every chainable op method was removed in favor of purely
// functional calls (tf.add(t, x), tf.reshape(t, shape)) -- confirmed
// directly against the installed version, and confirmed one-at-a-time via
// alignmentFailureReason in real testing ("e.toFloat is not a function",
// then "e.as4D is not a function", both from face-api's dom/NetInput.js).
// Rather than keep discovering these individually (each one only surfaces
// once execution gets past the previous fix), generically restore every
// tf.<op> as an instance method that forwards to the top-level function with
// `this` as the tensor argument -- that's the exact shape of the vast
// majority of removed methods. A handful of legacy names have no same-named
// top-level equivalent and need explicit mapping instead.
const patchLegacyTensorMethods = (faceapi: any): void => {
    const tf = faceapi?.tf;
    const TensorCtor = tf?.Tensor;
    if (!tf || !TensorCtor?.prototype) {
        return;
    }
    Object.keys(tf).forEach((key) => {
        if (typeof tf[key] === 'function' && typeof TensorCtor.prototype[key] !== 'function') {
            TensorCtor.prototype[key] = function forwardToTfOp(this: unknown, ...args: unknown[]) {
                return tf[key](this, ...args);
            };
        }
    });
    const legacyCasts: Array<['toFloat' | 'toInt' | 'toBool', 'float32' | 'int32' | 'bool']> = [
        ['toFloat', 'float32'],
        ['toInt', 'int32'],
        ['toBool', 'bool'],
    ];
    legacyCasts.forEach(([method, dtype]) => {
        if (typeof TensorCtor.prototype[method] !== 'function') {
            TensorCtor.prototype[method] = function toLegacyCast(this: { cast: (dtype: string) => unknown }) {
                return this.cast(dtype);
            };
        }
    });
    // as1D() is called with NO arguments in legacy usage ("flatten to 1D,
    // inferring the total size"), unlike as2D/as3D/.../as5D which always take
    // explicit dimensions (including the tf.reshape "-1 = infer" convention).
    // Forwarding an empty args array to tf.reshape(this, []) targets a
    // 0-rank scalar shape instead, which fails validation for any tensor
    // with more than 1 element -- confirmed via alignmentFailureReason
    // ("Size(136) must match the product of shape", the array stringifying
    // to '' because the target shape was [] -- face-api's postProcess calls
    // exactly this: `...as2D(1, 136).as1D()`).
    if (typeof TensorCtor.prototype.as1D !== 'function') {
        TensorCtor.prototype.as1D = function toLegacyFlatten(this: { size: number }) {
            return tf.reshape(this, [this.size]);
        };
    }
    ([2, 3, 4, 5] as const).forEach((rank) => {
        const method = `as${rank}D` as const;
        if (typeof TensorCtor.prototype[method] !== 'function') {
            TensorCtor.prototype[method] = function toLegacyRank(this: unknown, ...dims: number[]) {
                return tf.reshape(this, dims);
            };
        }
    });
    if (typeof TensorCtor.prototype.resizeBilinear !== 'function' && typeof tf.image?.resizeBilinear === 'function') {
        TensorCtor.prototype.resizeBilinear = function toLegacyResize(this: unknown, newShape: unknown, alignCorners?: boolean) {
            return tf.image.resizeBilinear(this, newShape, alignCorners);
        };
    }
};

export const loadFaceApiRuntimeBundle = async (): Promise<any> => {
    const faceapiModule = await import('face-api.js/build/es6/index.js');
    const faceapi = (faceapiModule as any)?.default || faceapiModule;
    patchLegacyTensorMethods(faceapi);
    return faceapi;
};
