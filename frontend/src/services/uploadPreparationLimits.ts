// How many photos prepare at once during an upload (decode a preview, PUT it, its thumbnail and its
// metadata packet). 2026-11-20: 13,088 preparation reports showed uploads capped near 110 files/min
// by decode (2 at a time) and PUT (2 at a time) in series, with admission sized to match. Raising one
// alone does nothing, so they move together. Each decode is a memory-heavy WASM instance, so
// phones, tablets and low-core machines keep the old numbers.
export interface PreparationLimits {
    /** Files holding a preparation slot (decoding, queued to decode, or PUTting). */
    admission: number;
    /** Decodes running at once. */
    decoders: number;
    /** Decodes allowed to wait; admission above decoders + queuedDecodes would skip preparation as "busy". */
    queuedDecodes: number;
    /** Derivative PUTs running at once. */
    puts: number;
}

export const BASE_PREPARATION_LIMITS: PreparationLimits = {admission: 5, decoders: 2, queuedDecodes: 4, puts: 2};
export const FAST_PREPARATION_LIMITS: PreparationLimits = {admission: 8, decoders: 4, queuedDecodes: 4, puts: 6};
const FAST_MIN_CORES = 6;

export const uploadPreparationLimits = (device: {parallel: boolean; constrained: boolean; cores?: number}): PreparationLimits => (
    device.parallel && !device.constrained && (device.cores === undefined || device.cores >= FAST_MIN_CORES)
        ? FAST_PREPARATION_LIMITS
        : BASE_PREPARATION_LIMITS
);
