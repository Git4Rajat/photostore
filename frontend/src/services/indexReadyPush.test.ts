import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { noteForegroundActivity } from './foregroundDemand';
import { __resetIndexDemandForTests, noteIndexActivity, setIndexDemandTab } from './indexDemand';
const mocks = vi.hoisted(() => ({ post: vi.fn(), library: 'A', user: 'u1', authenticated: true }));
vi.mock('./apiClient', () => ({ postTools: mocks.post }));
vi.mock('./passwordAuthClient', () => ({ getActiveLibraryFromToken: () => mocks.library }));
vi.mock('./pendingEdits', () => ({ pendingEditScope: () => `${mocks.user}/${mocks.library}` }));
vi.mock('./jobNotifications', () => ({ isLikelyAuthenticated: () => mocks.authenticated }));
const sockets: FakeSocket[] = [];
class FakeSocket {
    onopen?: () => void; onclose?: () => void; onerror?: () => void;
    onmessage?: (event: { data: string }) => void;
    close = vi.fn(() => this.onclose?.());
    constructor(public url: string) { sockets.push(this); }
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
beforeEach(() => {
    vi.useFakeTimers(); sockets.length = 0; mocks.library = 'A'; mocks.user = 'u1'; mocks.authenticated = true;
    noteForegroundActivity();
    // Tab-based demand (unlike noteIndexActivity()) never decays, so it keeps
    // every pre-existing test below -- which exercises the *foreground* gate,
    // not this one -- isolated from the index-demand gate's own 5-minute
    // window. The index-demand gate itself is covered separately below.
    setIndexDemandTab('tools');
    mocks.post.mockReset().mockResolvedValue({ available: true, url: 'wss://service/client', libraryId: 'A' });
    vi.stubGlobal('WebSocket', FakeSocket);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); __resetIndexDemandForTests(); });

describe('index publication push', () => {
    it('refreshes on connection and library-ready messages without user navigation', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const refresh = vi.fn(); const stop = startIndexReadyPush(refresh); await flush();
        sockets[0].onopen?.();
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'index-ready', libraryId: 'A' }) });
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'index-ready', libraryId: 'B' }) });
        expect(refresh).toHaveBeenCalledTimes(2);
        stop();
    });

    it('rejects delayed messages after switching libraries and renegotiates', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const refresh = vi.fn(); const stop = startIndexReadyPush(refresh); await flush();
        const old = sockets[0]; mocks.library = 'B';
        old.onmessage?.({ data: JSON.stringify({ type: 'index-ready', libraryId: 'A' }) });
        expect(refresh).not.toHaveBeenCalled();
        await vi.advanceTimersByTimeAsync(1000);
        expect(old.close).toHaveBeenCalled();
        expect(sockets).toHaveLength(2);
        stop();
    });

    it('reconnects and reconciles missed publications', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const refresh = vi.fn(); const stop = startIndexReadyPush(refresh); await flush();
        sockets[0].onopen?.(); sockets[0].close();
        await vi.advanceTimersByTimeAsync(1000);
        sockets[1].onopen?.();
        expect(refresh).toHaveBeenCalledTimes(2);
        stop(); await vi.advanceTimersByTimeAsync(300000);
        expect(sockets).toHaveLength(2);
    });

    it('does not negotiate unauthenticated sessions', async () => {
        mocks.authenticated = false;
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        expect(mocks.post).not.toHaveBeenCalled(); stop();
    });
});

describe('index publication push foreground-demand gate', () => {
    const hide = () => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
        document.dispatchEvent(new Event('visibilitychange'));
    };
    const show = () => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
        document.dispatchEvent(new Event('visibilitychange'));
    };
    afterEach(() => show());

    it('stops negotiating once backgrounded past the idle threshold', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        const callsBeforeHidden = mocks.post.mock.calls.length;
        hide();
        await vi.advanceTimersByTimeAsync(5 * 60000);
        const callsAtGate = mocks.post.mock.calls.length;
        await vi.advanceTimersByTimeAsync(5 * 60000);
        // Still polling every 30s, but not actually negotiating: no growth
        // past whatever had already been scheduled right at the gate.
        expect(mocks.post.mock.calls.length).toBe(callsAtGate);
        expect(callsAtGate).toBeGreaterThanOrEqual(callsBeforeHidden);
        stop();
    });

    it('performs an immediate authorized catch-up when the tab returns to foreground', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const refresh = vi.fn();
        const stop = startIndexReadyPush(refresh); await flush();
        hide();
        await vi.advanceTimersByTimeAsync(6 * 60000); // past the gate threshold
        const callsWhileHidden = mocks.post.mock.calls.length;
        show();
        await flush();
        expect(mocks.post.mock.calls.length).toBeGreaterThan(callsWhileHidden);
        stop();
    });

    it('pauses while hidden and catches up after a brief background dip', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        const beforeHidden=mocks.post.mock.calls.length;
        hide();
        await vi.advanceTimersByTimeAsync(60000); // well under the 5-minute gate
        expect(mocks.post.mock.calls.length).toBe(beforeHidden);
        show();
        await flush();
        sockets[sockets.length - 1].onopen?.();
        expect(mocks.post.mock.calls.length).toBe(beforeHidden+1);
        stop();
    });
});

describe('visible unattended upload page', () => {
    it('stops reconnects and invalidations after inactivity, then catches up on input', async () => {
        const { startIndexReadyPush }=await import('./indexReadyPush');
        const ready=vi.fn();const stop=startIndexReadyPush(ready);await flush();
        const old=sockets[0];old.onopen?.();
        await vi.advanceTimersByTimeAsync(301000);
        const requests=mocks.post.mock.calls.length;
        await vi.advanceTimersByTimeAsync(300000);
        old.onmessage?.({data:JSON.stringify({type:'index-ready',libraryId:'A'})});
        expect(mocks.post.mock.calls.length).toBe(requests);
        expect(ready).toHaveBeenCalledTimes(1);
        window.dispatchEvent(new Event('pointerdown'));await flush();
        expect(mocks.post.mock.calls.length).toBe(requests+1);
        stop();
    });
    it('ignores negotiation responses that arrive after the idle cutoff', async () => {
        let resolve!: (value: unknown) => void;
        mocks.post.mockReturnValue(new Promise(done=>{resolve=done;}));
        const {startIndexReadyPush}=await import('./indexReadyPush');
        const stop=startIndexReadyPush(vi.fn());
        await vi.advanceTimersByTimeAsync(301000);
        resolve({available:true,url:'wss://service/client',libraryId:'A'});await flush();
        expect(sockets).toHaveLength(0);
        stop();
    });
});

describe('index publication push index-demand gate', () => {
    // These tests exercise the *index-demand* dimension specifically, so they
    // must not inherit the outer beforeEach's setIndexDemandTab('tools'), nor
    // any leftover lastActivity from a previous test in this file.
    beforeEach(() => __resetIndexDemandForTests());

    it('does not negotiate with no recent activity and no tools/sharing tab open', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        expect(mocks.post).not.toHaveBeenCalled();
        stop();
    });

    it('negotiates once real activity is noted, then stops again after it decays', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        expect(mocks.post).not.toHaveBeenCalled();
        noteIndexActivity();
        await vi.advanceTimersByTimeAsync(0);
        expect(mocks.post).toHaveBeenCalledTimes(1);
        sockets[0].onopen?.();
        // The periodic self-close/reconnect (every 4 min) still fires once
        // more while inside the 5-minute activity window.
        await vi.advanceTimersByTimeAsync(4 * 60000);
        await vi.advanceTimersByTimeAsync(1000);
        expect(mocks.post.mock.calls.length).toBe(2);
        sockets[1]?.onopen?.();
        const callsAtActive = mocks.post.mock.calls.length;
        // The activity window has now long expired, so the next scheduled
        // self-close's reconnect attempt gets gated off before it negotiates.
        await vi.advanceTimersByTimeAsync(6 * 60000);
        expect(mocks.post.mock.calls.length).toBe(callsAtActive);
        stop();
    });

    it('negotiates for as long as the Tools/Sharing tab stays open, with no decay', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        setIndexDemandTab('sharing');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        expect(mocks.post).toHaveBeenCalled();
        const callsAtOpen = mocks.post.mock.calls.length;
        await vi.advanceTimersByTimeAsync(10 * 60000); // well past the activity window
        expect(mocks.post.mock.calls.length).toBeGreaterThanOrEqual(callsAtOpen);
        stop();
    });

    it('starts negotiating as soon as demand appears, without waiting for the next tick', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        expect(mocks.post).not.toHaveBeenCalled();
        setIndexDemandTab('tools');
        await flush();
        expect(mocks.post).toHaveBeenCalled();
        stop();
    });
});
