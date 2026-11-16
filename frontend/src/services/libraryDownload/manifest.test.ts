import { describe, expect, it } from 'vitest';
import { parseManifest } from './index';
import { formatBytes, formatDuration } from '../../mockups/prototype/components/DownloadLibrary';

const chunked = (text: string, size: number) => {
    const data = new TextEncoder().encode(text);
    let at = 0;
    return {
        read: async () => (at >= data.length ? null : data.slice(at, (at += size))),
        cancel: async () => undefined,
    };
};

describe('parseManifest', () => {
    const lines = '{"name":"å.jpg","blob":"a","path":"2024/05/å.jpg","size":3}\n{"name":"b","blob":"b","path":"undated/b"}\n';

    it('reads entries across chunk boundaries, including split multi-byte characters', async () => {
        const parsed = await parseManifest(chunked(`${lines}{"end":true,"files":2,"bytes":3}\n`, 5));
        expect(parsed.complete).toBe(true);
        expect(parsed.entries.map((e) => e.path)).toEqual(['2024/05/å.jpg', 'undated/b']);
    });

    it('reports a listing that ended without its trailer as incomplete', async () => {
        expect((await parseManifest(chunked(lines, 64))).complete).toBe(false);
    });
});

describe('formatting', () => {
    it('formats sizes and remaining time for people', () => {
        expect([formatBytes(0), formatBytes(999), formatBytes(1_250_000), formatBytes(150e9)])
            .toEqual(['0 B', '999 B', '1.3 MB', '150 GB']);
        expect([formatDuration(30), formatDuration(600), formatDuration(121_000)])
            .toEqual(['about a minute', '10 min', '33 h 37 min']);
    });
});
