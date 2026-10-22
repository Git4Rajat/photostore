
it.each([false,true])('audit: preserve versioned reconciliation through conflicting then matching unversioned reads (reload=%s)', async reload => {
    let j=await import('./pendingEdits');
    const scope=j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('photo','one.jpg',{rating:5}));
    j.applyPendingEdits('photo','one.jpg',{rating:5},'v2',scope,j.pendingEditReadTokens());
    j.applyPendingEdits('photo','one.jpg',{rating:2},'v3',scope,j.pendingEditReadTokens());
    const versionedCheck=j.sourceEditRequests();
    j.applyPendingEdits('photo','one.jpg',{rating:4},undefined,scope,j.pendingEditReadTokens());
    j.applyPendingEdits('photo','one.jpg',{rating:5},undefined,scope,j.pendingEditReadTokens());
    j.acceptSourceEdits(versionedCheck,[{kind:'photo',id:'one.jpg',values:{rating:2}}],scope);
    if(reload){vi.resetModules();j=await import('./pendingEdits');}
    // Even reading v3 again must allow the current source value to progress.
    j.applyPendingEdits('photo','one.jpg',{rating:2},'v3',scope,j.pendingEditReadTokens());
    j.acceptSourceEdits(j.sourceEditRequests(),[{kind:'photo',id:'one.jpg',values:{rating:2}}],scope);
    expect(j.applyPendingEdits('photo','one.jpg',{rating:2}).rating).toBe(2);
});
