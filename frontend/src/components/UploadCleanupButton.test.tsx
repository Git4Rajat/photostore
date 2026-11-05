import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const mocks=vi.hoisted(()=>({post:vi.fn(),postUpload:vi.fn(),scope:'library'}));
vi.mock('../services/apiClient',()=>({post:mocks.post,postUpload:mocks.postUpload}));
vi.mock('../services/sessionScope',()=>({sessionScope:()=>mocks.scope}));
import { UploadCleanupButton } from './UploadCleanupButton';
const done={deleted:0,trackingCleared:0,skipped:0,errors:[],more:false,nextCursor:''};
beforeEach(()=>{
    vi.clearAllMocks();mocks.scope='library';
    mocks.post.mockResolvedValue({pending:false});
    mocks.postUpload.mockResolvedValue(done);
});
afterEach(cleanup);

describe('Upload cleanup',()=>{
    it('runs only on click, reconciles Core, and cleans all pages',async()=>{
        mocks.postUpload.mockResolvedValueOnce({...done,deleted:25,more:true,nextCursor:'last.jpg'})
            .mockResolvedValueOnce({...done,deleted:3,trackingCleared:2});
        render(<UploadCleanupButton uploadActive={false}/>);
        expect(mocks.post).not.toHaveBeenCalled();
        fireEvent.click(screen.getByRole('button',{name:'Upload cleanup'}));
        await waitFor(()=>expect(screen.getByRole('status').textContent).toContain('28 unfinished upload records removed; 2 upload markers cleared.'));
        expect(mocks.post).toHaveBeenCalledWith('/api/upload-sessions/reconcile',{},expect.any(Object));
        expect(mocks.postUpload).toHaveBeenNthCalledWith(1,'/upload/cleanup',{after:''},expect.any(Object));
        expect(mocks.postUpload).toHaveBeenNthCalledWith(2,'/upload/cleanup',{after:'last.jpg'},expect.any(Object));
    });
    it('is disabled while an upload is active',()=>{
        render(<UploadCleanupButton uploadActive/>);
        const button=screen.getByRole('button',{name:'Upload cleanup'}) as HTMLButtonElement;
        expect(button.disabled).toBe(true);
        fireEvent.click(button);
        expect(mocks.post).not.toHaveBeenCalled();
    });
    it('reports partial failures and allows a fresh retry',async()=>{
        mocks.postUpload.mockResolvedValueOnce({...done,deleted:1,errors:[{filename:'bad.jpg',error:'failed'}]});
        render(<UploadCleanupButton uploadActive={false}/>);
        fireEvent.click(screen.getByRole('button',{name:'Upload cleanup'}));
        await waitFor(()=>expect(screen.getByRole('status').textContent).toContain('1 records could not be cleaned'));
        fireEvent.click(screen.getByRole('button',{name:'Upload cleanup'}));
        await waitFor(()=>expect(mocks.postUpload).toHaveBeenCalledTimes(2));
        await waitFor(()=>expect(screen.getByRole('status').textContent).toBe('0 unfinished upload records removed; 0 upload markers cleared.'));
    });
    it('keeps the action retryable after a network failure',async()=>{
        mocks.postUpload.mockRejectedValueOnce(new Error('Service unavailable'));
        render(<UploadCleanupButton uploadActive={false}/>);
        fireEvent.click(screen.getByRole('button',{name:'Upload cleanup'}));
        await waitFor(()=>expect(screen.getByRole('status').textContent).toContain('Service unavailable'));
        expect((screen.getByRole('button',{name:'Upload cleanup'}) as HTMLButtonElement).disabled).toBe(false);
    });
    it('waits for Core to finish recovery before deleting artifacts',async()=>{
        mocks.post.mockResolvedValue({pending:true});
        render(<UploadCleanupButton uploadActive={false}/>);
        fireEvent.click(screen.getByRole('button',{name:'Upload cleanup'}));
        await waitFor(()=>expect(screen.getByRole('status').textContent).toContain('Upload recovery is still finishing'));
        expect(mocks.postUpload).not.toHaveBeenCalled();
    });
    it('does not continue cleanup after a library switch',async()=>{
        mocks.postUpload.mockImplementationOnce(async()=>{mocks.scope='another';return {...done,more:true,nextCursor:'last.jpg'};});
        render(<UploadCleanupButton uploadActive={false}/>);
        fireEvent.click(screen.getByRole('button',{name:'Upload cleanup'}));
        await waitFor(()=>expect(mocks.postUpload).toHaveBeenCalledTimes(1));
        await act(async()=>{});
        expect(mocks.postUpload).toHaveBeenCalledTimes(1);
    });
});
