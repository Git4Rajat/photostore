    it('audit: a confirmed missing source album must not be re-injected forever', async () => {
        mocks.post.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:0}});
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async()=>{await store.createAlbum('Birthday');});
        mocks.get.mockImplementation(async (url:string)=>{if(url.startsWith('/albums/a1')) throw {status:404}; return {years:{}};});
        await act(async()=>{await store.openAlbum('a1');});
        expect(store.isAlbumMissing('a1')).toBe(true);
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums.some(a=>a.id==='a1')).toBe(false);
    });
    it('audit: a stale present index row cannot revert a confirmed membership count', async () => {
        mocks.post.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:0}});
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async()=>{await store.createAlbum('Birthday');});
        mocks.post.mockResolvedValue({success:true,added:['p0.jpg','p1.jpg'],album:{id:'a1',name:'Birthday',photoCount:2}});
        mocks.get.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:2},photos:[]});
        await act(async()=>{store.addPhotosToAlbum('a1',['p0.jpg','p1.jpg']);});
        expect(store.albums.find(a=>a.id==='a1')?.photoCount).toBe(2);
        mocks.albumsIndex.mockResolvedValue([{albumId:'a1',name:'Birthday',photoCount:0,coverFilename:''}]);
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums.find(a=>a.id==='a1')?.photoCount).toBe(2);
    });
    it('audit: a stale missing index row must not undo the newly created album name', async () => {
        mocks.post.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:0}});
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async()=>{await store.createAlbum('Birthday');});
        await act(async()=>{store.renameAlbum('a1','New name');});
        expect(store.albums.find(a=>a.id==='a1')?.name).toBe('New name');
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums.find(a=>a.id==='a1')?.name).toBe('New name');
    });
