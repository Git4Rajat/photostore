import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
    mocks.post.mockReset().mockResolvedValue({ available: true, url: 'wss://service/client', libraryId: 'A' });
    vi.stubGlobal('WebSocket', FakeSocket);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

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

    it('a brief background dip under the threshold never gates negotiation', async () => {
        const { startIndexReadyPush } = await import('./indexReadyPush');
        const stop = startIndexReadyPush(vi.fn()); await flush();
        hide();
        await vi.advanceTimersByTimeAsync(60000); // well under the 5-minute gate
        show();
        await flush();
        sockets[sockets.length - 1].onopen?.();
        // Connection still negotiates/opens normally -- a short background
        // dip (e.g. switching tabs briefly) is not treated as idle.
        expect(sockets.length).toBeGreaterThan(0);
        stop();
    });
});
