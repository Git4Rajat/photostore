import { beforeEach, describe, expect, it } from 'vitest';
import { pruneStaleWaveOperations } from './uploadWaveOperations';

const PREFIX = 'photostore.upload.wave-operation.v1:';
const base = (id: string) => PREFIX + JSON.stringify(['scope', id]);
const seed = (id: string, createdAt: number, receipts: number) => {
    localStorage.setItem(base(id), JSON.stringify({ id, scope: 'scope', createdAt, declaredCount: receipts }));
    for (let i = 0; i < receipts; i += 1) localStorage.setItem(`${base(id)}:${id}-${i}`, 'resolved');
};

describe('pruneStaleWaveOperations', () => {
    beforeEach(() => localStorage.clear());

    it('removes old operations with their receipts, keeps recent and protected ones', () => {
        const now = 10 * 86400000;
        seed('old', 0, 3);
        seed('fresh', now - 1000, 2);
        seed('kept', 0, 2);
        localStorage.setItem(`${base('gone')}:gone-0`, 'resolved');
        localStorage.setItem('unrelated', 'x');

        const removed = pruneStaleWaveOperations(7 * 86400000, 'kept', now);

        expect(removed).toBe(5);
        expect(localStorage.length).toBe(1 + 1 + 2 + 1 + 2);
        expect(localStorage.getItem(base('old'))).toBeNull();
        expect(localStorage.getItem(base('fresh'))).not.toBeNull();
        expect(localStorage.getItem(base('kept'))).not.toBeNull();
        expect(localStorage.getItem('unrelated')).toBe('x');
    });
});
