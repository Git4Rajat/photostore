
it('audit: equal dirty/clean manifest timestamps cannot justify replacing a newer payload', async () => {
    mocks.get.mockResolvedValue({available:true,indexUrl:'https://storage.test/sort',sourceVersion:'2026-02-02',updatedAt:'2026-02-02T00:00:00Z'});
    let finish!: (response:Response)=>void;
    let began=false;
    vi.stubGlobal('fetch',vi.fn().mockImplementationOnce(()=>{began=true;return new Promise(resolve=>{finish=resolve;});})
        .mockResolvedValue(new Response(JSON.stringify({...snapshot(),sourceVersion:'2026-02-02',rows:[{...snapshot().rows[0],rating:5}]}))));
    const tabA=await import('./localSortIndex');
    const old=tabA.getLocalSortIndex();
    await vi.waitFor(()=>expect(began).toBe(true));
    // Independent module state represents a second tab; IndexedDB remains shared.
    vi.resetModules(); const tabB=await import('./localSortIndex');
    expect((await tabB.getLocalSortIndex())?.[0].rating).toBe(5);
    finish(new Response(JSON.stringify({...snapshot(),rows:[{...snapshot().rows[0],rating:1}]})));
    await old;
    expect((await tabB.getLocalSortIndex())?.[0].rating).toBe(5);
    vi.resetModules(); const reloaded=await import('./localSortIndex');
    expect((await reloaded.getLocalSortIndex())?.[0].rating).toBe(5);
});
