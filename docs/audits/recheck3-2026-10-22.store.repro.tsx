
it('audit: album fallback revalidates a receipt acknowledged by a versioned index', async () => {
    const j=await import('../../services/pendingEdits');
    confirmPendingEdit(beginPendingEdit('album','a1',{name:'Saved'}));
    j.applyPendingEdits('album','a1',{name:'Saved'},'v1',pendingEditScope(),j.pendingEditReadTokens());
    expect(j.sourceEditRequests()).toEqual([]);
    mocks.albumsIndex.mockResolvedValue(null);
    mocks.get.mockImplementation(async(url:string)=>url==='/albums'?{albums:[{id:'a1',name:'Later',photoCount:0}]}:{years:{}});
    mocks.postTools.mockResolvedValue({libraryId:JSON.parse(pendingEditScope())[1],rows:[{kind:'album',id:'a1',values:{name:'Later'}}]});
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async()=>{await store.fetchAlbums(true);});
    await act(async()=>{await (await import('../../services/reconcilePendingEdits')).reconcilePendingEdits();});
    expect(store.albumById('a1')?.name).toBe('Later');
});
