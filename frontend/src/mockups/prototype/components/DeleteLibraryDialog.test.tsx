import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ verify: vi.fn(), request: vi.fn() }));
vi.mock('../../../services/libraryClient', () => {
    class WrongPasswordError extends Error {}
    return { WrongPasswordError, verifyLibraryPassword: mocks.verify, requestLibraryClean: mocks.request };
});
import * as library from '../../../services/libraryClient';
import DeleteLibraryDialog, { maskEmail } from './DeleteLibraryDialog';

const onClose = vi.fn();
const onSendStarted = vi.fn();
const renderDialog = (props: Partial<React.ComponentProps<typeof DeleteLibraryDialog>> = {}) => render(
    <DeleteLibraryDialog
        open onClose={onClose} onSendStarted={onSendStarted}
        libraryName="Verma family" totalPhotos={31} albumCount={2}
        passwordMode ownerEmail="rajat@example.test" otherMembers={[]}
        {...props}
    />,
);
const click = async (name: string) => { await act(async () => { fireEvent.click(screen.getByRole('button', { name })); }); };
const typePassword = (value: string) => fireEvent.change(screen.getByLabelText('Your password'), { target: { value } });

describe('Delete everything in this library', () => {
    beforeEach(() => {
        mocks.verify.mockReset(); mocks.request.mockReset(); onClose.mockReset(); onSendStarted.mockReset();
        mocks.request.mockResolvedValue({ status: 'pending', requiresAdditionalApproval: false, sentTo: ['r***@example.test'], expiresIn: 1800 });
    });
    afterEach(cleanup);

    it('says what goes, then asks for the password without sending anything', async () => {
        renderDialog();
        expect(screen.getByText(/all 31 photos and videos, 2 albums and everyone’s access/)).toBeTruthy();
        await click('Continue');
        expect(screen.getByText('Confirm with your password')).toBeTruthy();
        expect(mocks.request).not.toHaveBeenCalled();
    });

    it('answers a wrong password inside the dialog and keeps what was typed', async () => {
        mocks.verify.mockRejectedValue(new library.WrongPasswordError());
        renderDialog();
        await click('Continue');
        typePassword('hunter2');
        await click('Confirm');
        expect(screen.getByRole('alert').textContent).toContain('That password isn’t right. Try again. Nothing has been sent.');
        expect((screen.getByLabelText('Your password') as HTMLInputElement).value).toBe('hunter2');
        expect(mocks.request).not.toHaveBeenCalled();
        expect(onClose).not.toHaveBeenCalled();
    });

    it('disables the form while the password is being checked, so it cannot be sent twice', async () => {
        let finish: (proof: string) => void = () => {};
        mocks.verify.mockReturnValue(new Promise<string>((resolve) => { finish = resolve; }));
        renderDialog();
        await click('Continue');
        typePassword('correct horse');
        await click('Confirm');
        expect((screen.getByLabelText('Your password') as HTMLInputElement).disabled).toBe(true);
        expect(screen.getByRole('button', { name: /Checking/ })).toBeTruthy();
        await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Checking/ })); });
        expect(mocks.verify).toHaveBeenCalledTimes(1);
        await act(async () => { finish('proof-1'); });
        expect(screen.getByText('Check your inbox, and spam')).toBeTruthy();
    });

    it('shows the inbox step as soon as the password passes, and hands the send to the parent on OK', async () => {
        mocks.verify.mockResolvedValue('proof-1');
        const pending = new Promise(() => {});
        mocks.request.mockReturnValue(pending);
        renderDialog();
        await click('Continue');
        typePassword('correct horse');
        await click('Confirm');
        expect(mocks.request).toHaveBeenCalledWith('proof-1');
        expect(screen.getByText('r***@example.test')).toBeTruthy();
        expect(screen.getByText(/The link expires in 30 minutes. Nothing is deleted until you open it./)).toBeTruthy();
        expect(onSendStarted).not.toHaveBeenCalled();
        await click('OK');
        expect(onSendStarted).toHaveBeenCalledWith(pending, 'proof-1');
        expect(onClose).toHaveBeenCalled();
    });

    it('skips the password when the deployment does not use one', async () => {
        renderDialog({ passwordMode: false });
        await click('Continue');
        expect(mocks.verify).not.toHaveBeenCalled();
        expect(mocks.request).toHaveBeenCalledWith(undefined);
        expect(screen.getByText('Check your inbox, and spam')).toBeTruthy();
    });

    it('names the other member who will be asked to confirm', async () => {
        renderDialog({ passwordMode: false, otherMembers: ['asha@example.test'] });
        expect(screen.getByText('This library is shared. asha@example.test will be asked to confirm too.')).toBeTruthy();
        await click('Continue');
        expect(screen.getByText(/Because this library is shared, asha@example.test will be asked to confirm too/)).toBeTruthy();
        expect(screen.getByText(/Both links expire in 30 minutes. Nothing is deleted until you both open them./)).toBeTruthy();
    });

    it('masks an address the way the server does', () => {
        expect(maskEmail('rajat@example.test')).toBe('r***@example.test');
        expect(maskEmail('')).toBe('');
    });
});
