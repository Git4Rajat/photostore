
it('audit: person rollback preserves a name adopted by background source reconciliation', async () => {
    const actual = await vi.importActual<typeof import('../../services/faceService')>('../../services/faceService');
    mocks.labelPerson.mockImplementation(actual.default.labelPerson);
    mocks.get.mockResolvedValue({available:true,rows:[{personId:'p1',name:'Original',isNamed:true,faceCount:1}]});
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async()=>{await store.fetchPeople();});
    await act(async()=>{store.renamePerson('p1','First');});
    await waitFor(()=>expect(store.people[0]?.name).toBe('First'));
    mocks.postTools.mockResolvedValue({libraryId:JSON.parse(pendingEditScope())[1],rows:[{kind:'person',id:'p1',values:{name:'Remote'}}]});
    await act(async()=>{await (await import('../../services/reconcilePendingEdits')).reconcilePendingEdits();});
    expect(store.people[0]?.name).toBe('Remote');
    mocks.post.mockRejectedValue(new Error('offline'));
    await act(async()=>{store.renamePerson('p1','Attempt');});
    await waitFor(()=>expect(store.people[0]?.name).not.toBe('Attempt'));
    const j=await import('../../services/pendingEdits');
    expect(j.applyPendingEdits('person','p1',{name:'Original'}).name).toBe('Remote');
    expect(store.personById('p1')?.name).toBe('Remote');
    expect(store.people[0]?.name).toBe('Remote');
});
