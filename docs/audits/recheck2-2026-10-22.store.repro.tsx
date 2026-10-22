
it('audit: rotation is protected until the actual save succeeds', async () => {
    const j=await import('../../services/pendingEdits');
    let finish!: (value:unknown)=>void;
    mocks.post.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
    render(<StoreProvider><Probe /></StoreProvider>);
    await waitFor(()=>expect(store.photosLoading).toBe(false));
    await act(async()=>{
        store.applyPhotoRotation('p0.jpg',180);
        store.savePhotoRotation('p0.jpg',180,90);
    });
    expect(j.sourceEditRequests().some(edit=>edit.id==='p0.jpg')).toBe(false);
    expect(store.photoById('p0.jpg')?.rotation).toBe(180);
    await act(async()=>{finish({success:true});});
    expect(j.sourceEditRequests().some(edit=>edit.id==='p0.jpg')).toBe(true);
});

it('audit: failed overlapping rotations restore the saved baseline', async () => {
    const reject:Array<(error:Error)=>void>=[];
    mocks.post.mockImplementation(()=>new Promise((_resolve,fail)=>reject.push(fail)));
    render(<StoreProvider><Probe /></StoreProvider>);
    await waitFor(()=>expect(store.photosLoading).toBe(false));
    await act(async()=>{
        store.applyPhotoRotation('p0.jpg',180); store.savePhotoRotation('p0.jpg',180,90);
    });
    await act(async()=>{
        store.applyPhotoRotation('p0.jpg',270); store.savePhotoRotation('p0.jpg',270,180);
    });
    await act(async()=>{reject[0](new Error('offline'));});
    await waitFor(()=>expect(reject).toHaveLength(2));
    await act(async()=>{reject[1](new Error('offline'));});
    expect(store.photoById('p0.jpg')?.rotation).toBe(90);
    expect(store.photos.find(photo=>photo.id==='p0.jpg')?.rotation).toBe(90);
});

it('audit: failed person renames restore the saved name before another refetch', async () => {
    mocks.get.mockResolvedValue({available:true,rows:[{personId:'p1',name:'Original',isNamed:true,faceCount:1}]});
    const reject:Array<(error:Error)=>void>=[];
    mocks.labelPerson.mockImplementation(()=>new Promise((_resolve,fail)=>reject.push(fail)));
    render(<StoreProvider><Probe /></StoreProvider>);
    await act(async()=>{await store.fetchPeople();});
    await act(async()=>{store.renamePerson('p1','First');});
    await act(async()=>{store.renamePerson('p1','Second');});
    await act(async()=>{reject[0](new Error('offline'));});
    await act(async()=>{reject[1](new Error('offline'));});
    expect(store.people[0]?.name).toBe('Original');
});

it('audit: successful rotation wakes index recovery polling', async () => {
    mocks.post.mockResolvedValue({success:true});
    render(<StoreProvider><Probe /></StoreProvider>);
    await waitFor(()=>expect(store.photosLoading).toBe(false));
    await act(async()=>{store.applyPhotoRotation('p0.jpg',180);store.savePhotoRotation('p0.jpg',180,90);});
    expect(mocks.poll).toHaveBeenCalled();
});
