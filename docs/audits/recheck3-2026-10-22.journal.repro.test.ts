
it('audit: versioned photo convergence must not disable later paged observations', async () => {
    let j=await import('./pendingEdits');
    const scope=j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('photo','one.jpg',{rating:5}));
    j.applyPendingEdits('photo','one.jpg',{rating:5},'v1',scope,j.pendingEditReadTokens());
    expect(j.sourceEditRequests()).toEqual([]);
    vi.resetModules(); j=await import('./pendingEdits');
    // The library now uses the unversioned server-paged path; another client saved 2.
    j.applyPendingEdits('photo','one.jpg',{rating:2},undefined,scope,j.pendingEditReadTokens());
    j.acceptSourceEdits(j.sourceEditRequests(),[{kind:'photo',id:'one.jpg',values:{rating:2}}],scope);
    expect(j.applyPendingEdits('photo','one.jpg',{rating:2}).rating).toBe(2);
});
