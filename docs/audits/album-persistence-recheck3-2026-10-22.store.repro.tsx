// StoreProvider reproduction; insert inside store.gallery.test.tsx describe block.
    it('audit: a current source read must supersede an older create draft', async () => {
        mocks.post.mockResolvedValue({album:{id:'a1',name:'Birthday',photoCount:0}});
        render(<StoreProvider><Probe /></StoreProvider>);
        await act(async()=>{await store.createAlbum('Birthday');});
        // Another client has subsequently renamed it and added photos.
        mocks.albumsIndex.mockRejectedValueOnce(new Error('index unavailable'));
        mocks.get.mockResolvedValue({albums:[{id:'a1',name:'Remote name',photoCount:5}]});
        await act(async()=>{await store.fetchAlbums(true);});
        expect(store.albums.find(a=>a.id==='a1')).toMatchObject({name:'Remote name',photoCount:5});
    });
