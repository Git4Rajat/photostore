import { beforeEach, expect, it, vi } from 'vitest';
import { __resetAlbumOperationsForTests, beginAlbumCreate, beginAlbumDelete, applyAlbumOperationOverlay, reconcileAlbumCreateReceipts } from './albumOperations';
beforeEach(() => __resetAlbumOperationsForTests());
const render = (receipt: any) => ({id:receipt.id,...receipt.draft});
it('a newly created album stays deleted while its create receipt is pending', () => {
    beginAlbumCreate('a1',{name:'Birthday',photoCount:0});
    beginAlbumDelete('a1');
    expect(applyAlbumOperationOverlay([],render)).toEqual([]);
});
it('seeing a newer index once cannot let an older snapshot erase the create', () => {
    beginAlbumCreate('a1',{name:'Birthday',photoCount:0});
    reconcileAlbumCreateReceipts(new Set(['a1']));
    expect(applyAlbumOperationOverlay([],render).map(a=>a.id)).toEqual(['a1']);
});

it('audit: expiry cleanup must not skip a fresh persisted receipt during reload', async () => {
    vi.useFakeTimers();
    try {
        vi.setSystemTime(new Date('2026-10-20T10:00:00Z'));
        beginAlbumCreate('expired',{name:'Old',photoCount:0});
        vi.setSystemTime(new Date('2026-10-21T12:00:00Z'));
        beginAlbumCreate('fresh',{name:'New',photoCount:1});
        vi.resetModules();
        const reloaded=await import('./albumOperations');
        expect(reloaded.applyAlbumOperationOverlay([],render).map(a=>a.id)).toEqual(['fresh']);
    } finally { vi.useRealTimers(); }
});
