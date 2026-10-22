
    it.each(['people', 'albums'] as const)('audit: %s cache preserves newer payload across tabs', async (kind) => {
        mocks.get.mockResolvedValue({available:true,indexUrl:'https://storage.test/index',sourceVersion:'2026-02-02'});
        const row = (name: string) => ({personId:'p1',albumId:'a1',name});
        let finish!: (response:Response) => void;
        let began=false;
        vi.stubGlobal('fetch',vi.fn().mockImplementationOnce(()=>{began=true;return new Promise(resolve=>{finish=resolve;});})
            .mockResolvedValue(new Response(JSON.stringify({sourceVersion:'2026-02-02',rows:[row('New')]}))));
        const load = async () => kind === 'people'
            ? (await import('./localPeopleIndex')).getLocalPeopleIndex
            : (await import('./localAlbumsIndex')).getLocalAlbumsIndex;
        const tabA=await load();
        const old=tabA();
        await vi.waitFor(()=>expect(began).toBe(true));
        vi.resetModules(); const tabB=await load();
        expect((await tabB())?.[0].name).toBe('New');
        finish(new Response(JSON.stringify({sourceVersion:'2026-02-01',rows:[row('Old')]})));
        await old;
        vi.resetModules(); const reloaded=await load();
        expect((await reloaded())?.[0].name).toBe('New');
    });
