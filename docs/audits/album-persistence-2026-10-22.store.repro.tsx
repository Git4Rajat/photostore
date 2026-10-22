    it('audit: a legacy fallback cannot retire protection against a stale index', async () => {
        mocks.post.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:0}});
        mocks.get.mockResolvedValue({albums:[{id:'a1',name:'Birthday',photoCount:0}]});
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async()=>{await store.createAlbum('Birthday');});
        mocks.albumsIndex.mockRejectedValueOnce(new Error('index unavailable'));
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums.some(a=>a.id==='a1')).toBe(true);
        mocks.albumsIndex.mockResolvedValue([]);
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums.some(a=>a.id==='a1')).toBe(true);
    });
    it('audit: a failed explicit album read presents retry instead of endless resolving', async () => {
        mocks.get.mockImplementation(async (url:string) => {
            if(url.startsWith('/albums/a1')) throw {status:503};
            return {years:{}};
        });
        render(<StoreProvider><Probe /><AlbumsPage /></StoreProvider>);
        await act(async()=>{store.navigate('albums',{albumId:'a1'});});
        await waitFor(()=>expect(mocks.get.mock.calls.some(([url])=>String(url).startsWith('/albums/a1'))).toBe(true));
        await waitFor(()=>expect(store.isAlbumPhotosLoading('a1')).toBe(false));
        expect(screen.getByText('Opening album…')).toBeTruthy();
        expect(screen.queryByRole('button',{name:/retry/i})).not.toBeNull();
    });
    it('audit: purged albums remain hidden from older index snapshots', async () => {
        mocks.albumsIndex.mockResolvedValue([{albumId:'a1',name:'Old',photoCount:1,coverFilename:''}]);
        beginAlbumDelete('a1');
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums).toEqual([]);
        await act(async()=>{store.purgeAlbum('a1');});
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums).toEqual([]);
    });
    it('audit: a pending created album retains its saved membership count', async () => {
        mocks.post.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:0}});
        mocks.get.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:2},photos:[]});
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async()=>{await store.createAlbum('Birthday');});
        await act(async()=>{store.addPhotosToAlbum('a1',['p0.jpg','p1.jpg']);});
        expect(store.albums.find(a=>a.id==='a1')?.photoCount).toBe(2);
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums.find(a=>a.id==='a1')?.photoCount).toBe(2);
    });
