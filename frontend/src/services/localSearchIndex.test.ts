import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./apiClient', () => ({ get: vi.fn() }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: vi.fn(() => 'lib-1') }));

import { get } from './apiClient';
import { getLocalSearchIndex, invalidateLocalSearchIndex } from './localSearchIndex';

// Oversized ArrayBuffers (>480MB, see MAX_INDEX_BYTES) were the actual live
// trigger (microsvcpoc-dev, 2026-09-30): a 709MB decompressed index made
// TextDecoder.decode/JSON.parse throw RangeError every time, and the warm-up
// loop re-downloaded the 47.5MB gzipped blob every 15s forever, starving
// thumbnail requests. These tests exercise the real module code (not a
// test-only override) against an actual oversized buffer.
const OVERSIZED_BYTES = 480 * 1024 * 1024 + 1024;

describe('getLocalSearchIndex with an oversized index blob', () => {
    beforeEach(() => {
        invalidateLocalSearchIndex();
        vi.mocked(get).mockReset();
    });
    afterEach(() => {
        vi.unstubAllGlobals();
    });

    const stubOversizedFetch = () => {
        const fetchMock = vi.fn(async () => ({
            ok: true,
            arrayBuffer: async () => new ArrayBuffer(OVERSIZED_BYTES),
        }));
        vi.stubGlobal('fetch', fetchMock);
        return fetchMock;
    };

    it('fails gracefully (no uncaught RangeError) instead of crashing', async () => {
        vi.mocked(get).mockResolvedValue({
            available: true,
            indexUrl: 'https://storage.example/lexical-index/v1.json.gz',
            sourceVersion: 'v1',
        });
        stubOversizedFetch();

        const result = await getLocalSearchIndex();
        expect(result).toBeNull();
    });

    it('does not re-download the blob on a second call for the same sourceVersion', async () => {
        vi.mocked(get).mockResolvedValue({
            available: true,
            indexUrl: 'https://storage.example/lexical-index/v2.json.gz',
            sourceVersion: 'v2',
        });
        const fetchMock = stubOversizedFetch();

        await getLocalSearchIndex();
        expect(fetchMock).toHaveBeenCalledTimes(1);

        // cachedIndex stays null after a failed attempt, so a naive retry
        // loop (PrototypeApp.tsx's warm-up useEffect) calls getLocalSearchIndex()
        // again on the same sourceVersion -- this must NOT re-fetch the blob.
        await getLocalSearchIndex();
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(get).toHaveBeenCalledTimes(2);
    });

    it('retries the blob download once the server reports a new sourceVersion', async () => {
        vi.mocked(get).mockResolvedValue({
            available: true,
            indexUrl: 'https://storage.example/lexical-index/v3.json.gz',
            sourceVersion: 'v3',
        });
        const fetchMock = stubOversizedFetch();
        await getLocalSearchIndex();
        expect(fetchMock).toHaveBeenCalledTimes(1);

        vi.mocked(get).mockResolvedValue({
            available: true,
            indexUrl: 'https://storage.example/lexical-index/v4.json.gz',
            sourceVersion: 'v4',
        });
        await getLocalSearchIndex();
        expect(fetchMock).toHaveBeenCalledTimes(2);
    });
});
