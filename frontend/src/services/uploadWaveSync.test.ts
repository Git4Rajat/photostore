import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PersistedUploadSession } from '../types/browserProcessing';

const mocks = vi.hoisted(() => ({
    open: vi.fn(),
    register: vi.fn(),
    resolve: vi.fn(),
    close: vi.fn(),
}));

vi.mock('./uploadWaveClient', () => ({
    WAVE_SHADOW_MAX_PAGE: 20,
    openWaveShadowSession: mocks.open,
    registerWaveShadowItems: mocks.register,
    resolveWaveShadowItem: mocks.resolve,
    closeWaveShadowSession: mocks.close,
}));

import {
    __resetWaveShadowStateForTests,
    ensureWaveShadowSessionOpened,
    finalizeWaveShadowSession,
    resolveWaveShadowFile,
} from './uploadWaveSync';

function session(count: number, id: string): PersistedUploadSession {
    return {
        id, createdAt: 1700000000000,
        files: Array.from({ length: count }, (_, index) => ({
            key: `${id}:${index}`, name: `original-${index}.heic`, size: 50000,
            type: 'image/heic', lastModified: 1700000000000, uploadedBytes: 0,
            skippedDuplicate: false, status: 'pending' as const,
        })),
    };
}

beforeEach(() => {
    __resetWaveShadowStateForTests();
    vi.clearAllMocks();
    mocks.open.mockResolvedValue({ waveId: 'wave-1', manifestRevision: 'rev-0' });
    mocks.register.mockImplementation(async (_sessionId: string, items: unknown[]) =>
        ({ manifestRevision: `rev-register-${items.length}`, registeredCount: items.length }));
    mocks.resolve.mockResolvedValue({ manifestRevision: 'rev-resolve' });
    mocks.close.mockResolvedValue({ state: 'closed' });
});

afterEach(() => { vi.restoreAllMocks(); });

describe('ensureWaveShadowSessionOpened', () => {
    it('opens once and registers every item, paged at WAVE_SHADOW_MAX_PAGE', async () => {
        const s = session(45, 'sess-a');
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.open).toHaveBeenCalledTimes(1);
        expect(mocks.open).toHaveBeenCalledWith('sess-a', 45);
        expect(mocks.register).toHaveBeenCalledTimes(3); // 20 + 20 + 5
        expect(mocks.register.mock.calls[0][1]).toHaveLength(20);
        expect(mocks.register.mock.calls[2][1]).toHaveLength(5);
    });

    it('does not re-open or re-register on a second call once fully registered', async () => {
        const s = session(5, 'sess-b');
        await ensureWaveShadowSessionOpened(s);
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.open).toHaveBeenCalledTimes(1);
        expect(mocks.register).toHaveBeenCalledTimes(1);
    });

    it('resumes registration from where it left off after a failed page', async () => {
        const s = session(25, 'sess-c');
        mocks.register.mockResolvedValueOnce(null); // first page fails (network/disabled)
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.register).toHaveBeenCalledTimes(1);
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.register).toHaveBeenCalledTimes(3); // retried first page + second page
    });

    it('does nothing once the session is already closed', async () => {
        const s = session(1, 'sess-closed');
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        mocks.open.mockClear();
        await ensureWaveShadowSessionOpened(s);
        expect(mocks.open).not.toHaveBeenCalled();
    });
});

describe('resolveWaveShadowFile', () => {
    it('resolves a done, non-duplicate file as accepted with its filename', async () => {
        const s = session(1, 'sess-d');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: false });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-d', s.files[0].key, 'accepted', s.files[0].name);
    });

    it('resolves a done, duplicate file as duplicate', async () => {
        const s = session(1, 'sess-e');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'done', skippedDuplicate: true });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-e', s.files[0].key, 'duplicate', s.files[0].name);
    });

    it('resolves a failed file as failed without a filename', async () => {
        const s = session(1, 'sess-f');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'failed' });
        expect(mocks.resolve).toHaveBeenCalledWith('sess-f', s.files[0].key, 'failed', undefined);
    });

    it('does not call resolve for a non-terminal status', async () => {
        const s = session(1, 'sess-g');
        await resolveWaveShadowFile(s, { ...s.files[0], status: 'uploading' });
        expect(mocks.resolve).not.toHaveBeenCalled();
    });
});

describe('finalizeWaveShadowSession', () => {
    it('resolves every outstanding file as cancelled, then closes', async () => {
        const s = session(3, 'sess-h');
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        s.files[1].status = 'failed';
        // files[2] stays 'pending' -- e.g. the upload was stopped mid-transfer.
        await finalizeWaveShadowSession(s);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[0].key, 'accepted', s.files[0].name);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[1].key, 'failed', undefined);
        expect(mocks.resolve).toHaveBeenCalledWith('sess-h', s.files[2].key, 'cancelled', undefined);
        expect(mocks.close).toHaveBeenCalledTimes(1);
    });

    it('does nothing if the session was never opened', async () => {
        const s = session(1, 'sess-i');
        await finalizeWaveShadowSession(s);
        expect(mocks.resolve).not.toHaveBeenCalled();
        expect(mocks.close).not.toHaveBeenCalled();
    });

    it('does not close when no manifest revision was ever observed', async () => {
        const s = session(1, 'sess-j');
        mocks.open.mockResolvedValueOnce({ waveId: 'wave-1', manifestRevision: undefined });
        mocks.register.mockResolvedValueOnce(null); // shadow call failed
        mocks.resolve.mockResolvedValue(null); // shadow call failed
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        expect(mocks.close).not.toHaveBeenCalled();
    });

    it('is idempotent -- calling it twice only closes once', async () => {
        const s = session(1, 'sess-k');
        await ensureWaveShadowSessionOpened(s);
        s.files[0].status = 'done';
        await finalizeWaveShadowSession(s);
        await finalizeWaveShadowSession(s);
        expect(mocks.close).toHaveBeenCalledTimes(1);
    });
});
