// Append to a temporary copy of store.gallery.test.tsx in the same directory to reuse its fixtures.
it('finds a pending rename beyond the enrichment budget when the search matches only that name', async () => {
    __resetPendingEditsForTests();
    mocks.postTools.mockReset().mockRejectedValue(new Error('Source temporarily unavailable'));
    mocks.index.mockResolvedValue(rows()); mocks.albumsIndex.mockResolvedValue([]);
    mocks.token.mockResolvedValue({ baseUrl: 'https://storage.invalid', sas: 'token' });
    for (let n = 0; n < 101; n++) confirmPendingEdit(beginPendingEdit('person', 'pending-' + n, { name: 'New Name ' + n }));
    mocks.get.mockImplementation(async (url: string) => {
        const ids = new URL(url, 'https://test.invalid').searchParams.get('ids');
        if (ids) return { available: true, sourceVersion: 'v1', rows: ids.split(',').map(personId => ({
            personId, name: 'Unnamed 1', isNamed: false, faceCount: 1,
        })) };
        return { available: true, sourceVersion: 'v1', rows: [], total: 101 };
    });
    render(<StoreProvider><Probe /></StoreProvider>);
    let result: Awaited<ReturnType<typeof store.searchPeople>> = [];
    await act(async () => { result = await store.searchPeople('New Name 100'); });
    expect(result.map(person => person.id)).toContain('pending-100');
    cleanup();
});
