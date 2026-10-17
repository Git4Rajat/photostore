import { describe, expect, it } from 'vitest';

import { FILE_ACCEPT_FILTER, getMediaKind, isRawFilename, isHeicFilename, requiresBackendPreview, shouldSkipConvertedRawPreview } from './photoDisplay';

it.each(['braw', 'r3d', 'crm', 'nev'])('cinema video %s never uses the still-image RAW fallback', extension => {
    expect(getMediaKind('clip.' + extension)).toBe('VIDEO');
    expect(isRawFilename('clip.' + extension)).toBe(false);
    expect(requiresBackendPreview('clip.' + extension)).toBe(false);
});

it.each(['heic', 'heif', 'hif', 'HIF'])('recognizes %s as HEIF with a backend preview fallback', extension => {
    expect(isHeicFilename(`image.${extension}`)).toBe(true);
    expect(requiresBackendPreview(`image.${extension}`)).toBe(true);
    expect(FILE_ACCEPT_FILTER).toContain('.' + extension.toLowerCase());
});

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
