
it('audit: album rollback uses the latest authoritative name after source reconciliation', async () => {
    const j=await import('../../services/pendingEdits');
    mocks.albumsIndex.mockResolvedValue([{albumId:'a1',name:'Original',photoCount:0,coverFilename:''}]);
    mocks.post.mockResolvedValue({success:true});
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async()=>{await store.fetchAlbums(true);});
    await act(async()=>{store.renameAlbum('a1','First');});
    await waitFor(()=>expect(store.albumById('a1')?.name).toBe('First'));
    // Another client renames it; authoritative reconciliation updates the receipt.
    j.acceptSourceEdits(j.sourceEditRequests(),[{kind:'album',id:'a1',values:{name:'Remote'}}],pendingEditScope());
    mocks.albumsIndex.mockResolvedValue([{albumId:'a1',name:'Remote',photoCount:0,coverFilename:''}]);
    await act(async()=>{await store.fetchAlbums(true);});
    expect(store.albums[0]?.name).toBe('Remote');
    mocks.post.mockRejectedValue(new Error('offline'));
    await act(async()=>{store.renameAlbum('a1','Attempt');});
    await waitFor(()=>expect(store.albumById('a1')?.name).toBe('Remote'));
    // The sidebar consumes albums directly, while detail uses albumById.
    expect(store.albums[0]?.name).toBe('Remote');
});
