
it('audit: a fresh matching unversioned page must fence an initial source check too', async () => {
    const j=await import('./pendingEdits');
    const scope=j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('photo','a.jpg',{rating:5}));
    const oldSourceCheck=j.sourceEditRequests();
    j.applyPendingEdits('photo','a.jpg',{rating:5},undefined,scope,j.pendingEditReadTokens());
    j.acceptSourceEdits(oldSourceCheck,[{kind:'photo',id:'a.jpg',values:{rating:2}}],scope);
    expect(j.applyPendingEdits('photo','a.jpg',{rating:5}).rating).toBe(5);
});

it('audit: a matching unversioned page cannot prove a newer versioned conflict resolved', async () => {
    const j=await import('./pendingEdits');
    const scope=j.pendingEditScope();
    j.confirmPendingEdit(j.beginPendingEdit('person','p1',{name:'Alice'}));
    j.applyPendingEdits('person','p1',{name:'Alice'},'v2',scope,j.pendingEditReadTokens());
    j.applyPendingEdits('person','p1',{name:'Bob'},'v3',scope,j.pendingEditReadTokens());
    const source=j.sourceEditRequests();
    j.applyPendingEdits('person','p1',{name:'Alice'},undefined,scope,j.pendingEditReadTokens());
    j.acceptSourceEdits(source,[{kind:'person',id:'p1',values:{name:'Bob'}}],scope);
    // If new page evidence fences that response, it must demand a new check.
    j.acceptSourceEdits(j.sourceEditRequests(),[{kind:'person',id:'p1',values:{name:'Bob'}}],scope);
    expect(j.applyPendingEdits('person','p1',{name:'Old'}).name).toBe('Bob');
});
