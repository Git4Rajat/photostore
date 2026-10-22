import { beforeEach, expect, it, vi } from 'vitest';
import type { AlbumCreateReceipt } from './albumOperations';
import { rollbackAlbumDelete, hasAlbumDeleteTombstone, albumCreateReadTokens, observeAlbumSourceRows, updateAlbumCreateReceipt, __resetAlbumOperationsForTests, beginAlbumCreate, beginAlbumDelete, applyAlbumOperationOverlay, reconcileAlbumCreateReceipts } from './albumOperations';
beforeEach(() => __resetAlbumOperationsForTests());
const render = (receipt: AlbumCreateReceipt) => ({id:receipt.id,...receipt.draft});
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

it('retains refreshed source fields through an older or missing index', () => {
    beginAlbumCreate('a1', {name: 'Birthday', photoCount: 0});
    const tokens = albumCreateReadTokens();
    observeAlbumSourceRows([{id: 'a1', name: 'Remote', photoCount: 5}], tokens);
    expect(applyAlbumOperationOverlay([{id: 'a1', name: 'Birthday', photoCount: 0}], render)[0])
        .toMatchObject({name: 'Remote', photoCount: 5});
    expect(applyAlbumOperationOverlay([], render)[0]).toMatchObject({name: 'Remote', photoCount: 5});
});

it('does not overwrite a save confirmed after a source read started', () => {
    beginAlbumCreate('a1', {name: 'Birthday', photoCount: 0});
    const tokens = albumCreateReadTokens();
    updateAlbumCreateReceipt('a1', {photoCount: 2});
    observeAlbumSourceRows([{id: 'a1', name: 'Birthday', photoCount: 0}], tokens);
    expect(applyAlbumOperationOverlay([], render)[0].photoCount).toBe(2);
});

it('does not use a source request started before creation as confirmation', () => {
    const tokens = albumCreateReadTokens();
    beginAlbumCreate('a1', {name: 'Birthday', photoCount: 2});
    observeAlbumSourceRows([{id: 'a1', name: 'Old', photoCount: 0}], tokens);
    expect(applyAlbumOperationOverlay([], render)[0]).toMatchObject({name: 'Birthday', photoCount: 2});
});

it('accepts strictly newer index rows and retains them through older snapshots', () => {
    beginAlbumCreate('a1', {name: 'Birthday', photoCount: 2, updatedAt: '2026-10-22T10:00:00.000001+00:00'});
    const newer: AlbumCreateReceipt['draft'] & {id: string} = {id: 'a1', name: 'Remote', photoCount: 5, updatedAt: '2026-10-22T10:00:00.000002+00:00'};
    expect(applyAlbumOperationOverlay([newer], render)[0]).toMatchObject({name: 'Remote', photoCount: 5});
    expect(applyAlbumOperationOverlay([{...newer, name: 'Old', photoCount: 0, updatedAt: '2026-10-22T09:59:59+00:00'}], render)[0])
        .toMatchObject({name: 'Remote', photoCount: 5});
    expect(applyAlbumOperationOverlay([], render)[0]).toMatchObject({name: 'Remote', photoCount: 5});
});

it('does not regress a revision floor on a lagging source response', () => {
    beginAlbumCreate('a1', {name: 'Birthday', photoCount: 2, updatedAt: '2026-10-22T10:00:01+00:00'});
    observeAlbumSourceRows([{id: 'a1', name: 'Old', photoCount: 0, updatedAt: '2026-10-22T10:00:00+00:00'}], albumCreateReadTokens());
    expect(applyAlbumOperationOverlay([], render)[0]).toMatchObject({name: 'Birthday', photoCount: 2});
});


it('does not let a failed delete remove previous or superseding deletion evidence', () => {
    beginAlbumDelete('existing');
    expect(rollbackAlbumDelete(beginAlbumDelete('existing'))).toBe(false);
    expect(hasAlbumDeleteTombstone('existing')).toBe(true);
    const older = beginAlbumDelete('newer');
    beginAlbumDelete('newer');
    expect(rollbackAlbumDelete(older)).toBe(false);
    expect(hasAlbumDeleteTombstone('newer')).toBe(true);
});


it.each([true, false])('clears two overlapping failed deletes in either completion order (old first: %s)', (oldFirst) => {
    const older = beginAlbumDelete('a1');
    const newer = beginAlbumDelete('a1');
    rollbackAlbumDelete(oldFirst ? older : newer);
    rollbackAlbumDelete(oldFirst ? newer : older);
    expect(hasAlbumDeleteTombstone('a1')).toBe(false);
});


it('does not roll back a tombstone using an unregistered attempt object', () => {
    const attempt = beginAlbumDelete('a1');
    expect(rollbackAlbumDelete({...attempt})).toBe(false);
    expect(hasAlbumDeleteTombstone('a1')).toBe(true);
    expect(rollbackAlbumDelete(attempt)).toBe(true);
});
