
it('audit: a newer matching unversioned read must invalidate an older source response', async () => {
    const j=await import('./pendingEdits');
    const scope=j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('photo','a.jpg',{rating:5}));
    j.applyPendingEdits('photo','a.jpg',{rating:2},undefined,scope,j.pendingEditReadTokens());
    const oldSourceCheck=j.sourceEditRequests();
    // Source was 2 when that check ran; a later fresh page now sees 5 again.
    j.applyPendingEdits('photo','a.jpg',{rating:5},undefined,scope,j.pendingEditReadTokens());
    j.acceptSourceEdits(oldSourceCheck,[{kind:'photo',id:'a.jpg',values:{rating:2}}],scope);
    expect(j.applyPendingEdits('photo','a.jpg',{rating:5}).rating).toBe(5);
});

it('audit: a rotation still awaiting its save must not be reconciled back to the old source', async () => {
    const index=await import('./localSortIndex');
    await index.getLocalSortIndex();
    // PhotoViewer applies this patch before dispatching setPhotoRotation.
    index.patchLocalSortIndexRow('a.jpg',{rotation:180});
    mocks.postTools.mockResolvedValue({libraryId:'library-A',rows:[{kind:'photo',id:'a.jpg',values:{rotation:90}}]});
    await (await import('./reconcilePendingEdits')).reconcilePendingEdits();
    expect((await index.getLocalSortIndex())?.[0].gallery?.rotation).toBe(180);
});

it('audit: an invalidated download cannot replace the newer persistent snapshot', async () => {
    mocks.get.mockResolvedValue({available:true,indexUrl:'https://storage.test/sort',sourceVersion:'2026-02-02'});
    let index=await import('./localSortIndex');
    let finish!: (response:Response)=>void;
    let began=false;
    vi.stubGlobal('fetch',vi.fn().mockImplementationOnce(()=>{began=true;return new Promise(resolve=>{finish=resolve;});})
        .mockResolvedValue(new Response(JSON.stringify({...snapshot(),sourceVersion:'2026-02-02',rows:[{...snapshot().rows[0],rating:5}]}))));
    const old=index.getLocalSortIndex();
    await vi.waitFor(()=>expect(began).toBe(true));
    index.invalidateLocalSortIndex();
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
    finish(new Response(JSON.stringify({...snapshot(),rows:[{...snapshot().rows[0],rating:1}]})));
    await old;
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
    vi.resetModules(); index=await import('./localSortIndex');
    expect((await index.getLocalSortIndex())?.[0].rating).toBe(5);
});
