import { describe, expect, it } from 'vitest';

import { shouldSkipConvertedRawPreview } from './photoDisplay';

describe('shouldSkipConvertedRawPreview', () => {
    it('does not skip on a normal (non-forced) pass', () => {
        expect(shouldSkipConvertedRawPreview(false, new Set(['preview']))).toBe(false);
        expect(shouldSkipConvertedRawPreview(false, null)).toBe(false);
    });

    it('skips a forced re-run that includes the preview step', () => {
        expect(shouldSkipConvertedRawPreview(true, new Set(['preview']))).toBe(true);
    });

    it('skips a forced re-run that includes the thumbnail step', () => {
        expect(shouldSkipConvertedRawPreview(true, new Set(['thumbnail']))).toBe(true);
    });

    it('skips a forced run with no explicit step filter (all steps)', () => {
        expect(shouldSkipConvertedRawPreview(true, null)).toBe(true);
    });

    it('does not skip a forced re-run of unrelated steps', () => {
        expect(shouldSkipConvertedRawPreview(true, new Set(['exif', 'ocr']))).toBe(false);
    });
});
