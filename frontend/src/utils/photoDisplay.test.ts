import { describe, expect, it } from 'vitest';

import {
    FILE_ACCEPT_FILTER,
    SUPPORTED_VIDEO_EXTENSIONS,
    UPLOADABLE_EXTENSIONS,
    getMediaKind,
    isRawFilename,
    isHeicFilename,
    isUploadableFilename,
    requiresBackendPreview,
    shouldSkipConvertedRawPreview,
} from './photoDisplay';

describe('isUploadableFilename', () => {
    it.each(['IMG_0001.HEIC', 'IMG_0001.jpg', 'clip.MOV', 'shot.cr3', 'scan.tiff', 'frame.ari'])('accepts %s', name => {
        expect(isUploadableFilename(name)).toBe(true);
    });

    it.each(['IMG_O9346.aae', 'IMG_0001.xmp', '.DS_Store', 'notes.txt', 'no-extension'])('rejects %s', name => {
        expect(isUploadableFilename(name)).toBe(false);
    });

    it('covers every picker extension the app can display', () => {
        const pickerExtensions = FILE_ACCEPT_FILTER.split(',')
            .filter(entry => entry.startsWith('.'))
            .map(entry => entry.slice(1));
        for (const extension of [...pickerExtensions, ...SUPPORTED_VIDEO_EXTENSIONS]) {
            expect(UPLOADABLE_EXTENSIONS.has(extension)).toBe(true);
        }
    });
});

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
