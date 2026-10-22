// Append this test to a copy of store.gallery.test.tsx to reuse its controlled service fixtures.
it('opening an older album request cannot undo a successful rename', async () => {
    mocks.albumsIndex.mockResolvedValue([{ albumId: 'a1', name: 'Old', photoCount: 0, coverFilename: '' }]);
    mocks.index.mockResolvedValue(rows());
    mocks.token.mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' });
    mocks.post.mockResolvedValue({ success: true });
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async () => { store.reloadAlbums(); });
    await waitFor(() => expect(store.albumById('a1')?.name).toBe('Old'));
    let finish!: (value: unknown) => void;
    mocks.get.mockImplementation((url: string) => url.startsWith('/albums/a1?')
        ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ years: {} }));
    act(() => { store.openAlbum('a1'); });
    await waitFor(() => expect(finish).toBeDefined());
    await act(async () => { store.renameAlbum('a1', 'New'); });
    await waitFor(() => expect(store.albumById('a1')?.name).toBe('New'));
    await act(async () => { finish({ album: { id: 'a1', name: 'Old', photoCount: 0 }, photos: [] }); });
    expect(store.albumById('a1')?.name).toBe('New');
    cleanup();
});
