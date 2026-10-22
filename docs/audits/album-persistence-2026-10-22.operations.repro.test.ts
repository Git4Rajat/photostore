import { beforeEach, expect, it } from 'vitest';
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
