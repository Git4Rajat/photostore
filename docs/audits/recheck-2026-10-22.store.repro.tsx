
it('audit: fallback album reads must revalidate a later server rename', async () => {
    __resetPendingEditsForTests();
    const j = await import('../../services/pendingEdits');
    confirmPendingEdit(beginPendingEdit('album', 'a1', { name: 'Saved' }));
    j.acceptSourceEdits(j.sourceEditRequests(), [{kind:'album',id:'a1',values:{name:'Saved'}}], pendingEditScope());
    mocks.albumsIndex.mockResolvedValue(null);
    mocks.get.mockImplementation(async (url: string) => url === '/albums'
        ? {albums:[{id:'a1',name:'Later',photoCount:0}]} : {years:{}});
    mocks.postTools.mockResolvedValue({libraryId:JSON.parse(pendingEditScope())[1],rows:[{kind:'album',id:'a1',values:{name:'Later'}}]});
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async () => { await store.fetchAlbums(true); });
    await act(async () => { await (await import('../../services/reconcilePendingEdits')).reconcilePendingEdits(); });
    expect(store.albumById('a1')?.name).toBe('Later');
});

it('audit: two failed album renames restore the last saved name', async () => {
    mocks.albumsIndex.mockResolvedValue([{albumId:'a1',name:'Original',photoCount:0,coverFilename:''}]);
    const reject: Array<(error: Error) => void> = [];
    mocks.post.mockImplementation(() => new Promise((_resolve, fail) => reject.push(fail)));
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async () => { await store.fetchAlbums(true); });
    await act(async () => { store.renameAlbum('a1','First'); });
    await act(async () => { store.renameAlbum('a1','Second'); });
    await act(async () => { reject[0](new Error('offline')); });
    await waitFor(() => expect(reject).toHaveLength(2));
    await act(async () => { reject[1](new Error('offline')); });
    expect(store.albumById('a1')?.name).toBe('Original');
});

it('audit: two failed person renames cannot retain the first failed edit', async () => {
    mocks.get.mockResolvedValue({available:true,rows:[{personId:'p1',name:'Original',isNamed:true,faceCount:1}]});
    const reject: Array<(error: Error) => void> = [];
    mocks.labelPerson.mockImplementation(() => new Promise((_resolve,fail) => reject.push(fail)));
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async () => { await store.fetchPeople(); });
    await act(async () => { store.renamePerson('p1','First'); });
    await act(async () => { store.renamePerson('p1','Second'); });
    await act(async () => { reject[0](new Error('offline')); });
    await act(async () => { reject[1](new Error('offline')); });
    await act(async () => { await store.fetchPeople(); });
    expect(store.people[0]?.name).toBe('Original');
});
