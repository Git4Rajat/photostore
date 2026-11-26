import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ invite: vi.fn(), removeMember: vi.fn(), confirm: vi.fn(), prompt: vi.fn(), transfer: vi.fn(), verify: vi.fn(), toast: vi.fn(), accept: vi.fn(), decline: vi.fn() }));
vi.mock('../store', () => ({
    useStore: () => ({
        members: [
            { userId: 'u1', email: 'owner@example.test', isOwner: true, isSelf: true },
            { userId: 'u2', email: 'asha@example.test', isOwner: false, isSelf: false },
        ],
        pendingInvites: [{ inviteId: 'i1', email: 'dev@example.test', targetType: 'join', expiresAt: Date.now() + 20 * 3_600_000 }],
        receivedInvites: [{ inviteId: 'r1', libraryId: 'lib9', libraryName: 'Rao family', invitedBy: 'priya@example.test', expiresAt: Date.now() + 20 * 3_600_000 }],
        libraryName: 'Verma family', isOwner: true, maxMembers: 15, membersLoading: false, totalPhotos: 31, albums: [],
        fetchMembers: vi.fn(async () => {}), invite: mocks.invite, revokeInvite: vi.fn(), removeMember: mocks.removeMember,
        renameLibrary: vi.fn(), toast: mocks.toast,
    }),
}));
vi.mock('../../../services/backgroundRequestQueue', () => ({ enqueueBackgroundRequest: () => Promise.resolve() }));
vi.mock('../../../components/shared/dialogs', () => ({ confirmDialog: mocks.confirm, promptDialog: mocks.prompt }));
vi.mock('../../../services/libraryClient', () => ({ transferOwnership: mocks.transfer, verifyLibraryPassword: mocks.verify, acceptReceivedInvite: mocks.accept, declineReceivedInvite: mocks.decline }));

import SharingPage, { inviteEnds } from './SharingPage';

describe('Library and sharing', () => {
    beforeEach(() => { mocks.invite.mockReset(); mocks.removeMember.mockReset(); mocks.confirm.mockReset(); mocks.prompt.mockReset(); mocks.transfer.mockReset(); mocks.verify.mockReset(); mocks.toast.mockReset(); mocks.accept.mockReset(); mocks.decline.mockReset(); });
    afterEach(cleanup);

    it('spells out both invite choices and sends the one picked', () => {
        render(<SharingPage />);
        const form = document.querySelector('.pt-library-invite') as HTMLElement;
        expect(form.textContent).toContain('Join Verma family');
        expect(form.textContent).toContain('They see all 31 items here');
        expect(form.textContent).toContain('ends after 72 hours');
        fireEvent.change(form.querySelector('input[type=email]')!, { target: { value: 'neha@example.test' } });
        fireEvent.click(form.querySelectorAll('input[type=radio]')[1]);
        fireEvent.click(form.querySelector('button[type=submit]')!);
        expect(mocks.invite).toHaveBeenCalledWith('neha@example.test', 'fresh');
    });

    it('says when a pending invite ends', () => {
        render(<SharingPage />);
        expect(screen.getByText(/Joining Verma family · ends in 20 hours/)).toBeTruthy();
        const now = 1_000_000_000_000;
        expect(inviteEnds(now + 50 * 3_600_000, now)).toBe('ends in 3 days');
        expect(inviteEnds((now - 1) / 1000, now)).toBe('ended');
    });

    it('lists invitations sent to this account and lets them be declined', async () => {
        mocks.decline.mockResolvedValue(undefined);
        render(<SharingPage />);
        expect(screen.getByText('Invitations for you')).toBeTruthy();
        expect(screen.getByText('Rao family')).toBeTruthy();
        expect(screen.getByText(/From priya@example.test · ends in 20 hours/)).toBeTruthy();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Decline' })); });
        expect(mocks.decline).toHaveBeenCalledWith({ libraryId: 'lib9', inviteId: 'r1', libraryName: 'Rao family', invitedBy: 'priya@example.test', expiresAt: expect.any(Number) });
    });

    it('asks before removing a member', async () => {
        render(<SharingPage />);
        mocks.confirm.mockResolvedValue(false);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Remove' })); });
        expect(mocks.removeMember).not.toHaveBeenCalled();
        mocks.confirm.mockResolvedValue(true);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Remove' })); });
        expect(mocks.removeMember).toHaveBeenCalledWith('u2');
    });

    it('makes a member the owner only after confirmation and the typed email', async () => {
        mocks.confirm.mockResolvedValueOnce(false);
        render(<SharingPage />);
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Make owner' })); });
        expect(mocks.transfer).not.toHaveBeenCalled();
        mocks.confirm.mockResolvedValueOnce(true);
        mocks.prompt.mockResolvedValueOnce('asha@example.test');
        mocks.transfer.mockRejectedValueOnce(new Error('nope'));
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Make owner' })); });
        expect(mocks.transfer).toHaveBeenCalledWith('u2', { confirmEmail: 'asha@example.test' });
        expect(mocks.toast).toHaveBeenCalledWith('nope', undefined, undefined, 'error');
    });
});
