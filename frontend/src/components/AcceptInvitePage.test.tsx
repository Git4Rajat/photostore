import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ info: vi.fn(), accept: vi.fn() }));
vi.mock('../services/libraryClient', () => ({ getInviteInfo: mocks.info, acceptInvite: mocks.accept }));
import AcceptInvitePage from './AcceptInvitePage';

const mount = async (url = '/accept-invite?token=t') => {
    render(<MemoryRouter initialEntries={[url]}><AcceptInvitePage /></MemoryRouter>);
    await act(async () => { await Promise.resolve(); });
};
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
afterEach(() => { cleanup(); mocks.info.mockReset(); mocks.accept.mockReset(); });

it('draws "Set up your library" for an invited person starting their own library', async () => {
    mocks.info.mockResolvedValue({ valid: true, email: 'neha@example.com', targetType: 'fresh', needsPassword: true });
    mocks.accept.mockResolvedValue({ activeLibraryId: 'x', newAccount: true });
    await mount();
    expect(screen.getByRole('heading', { name: 'Set up your library' })).toBeTruthy();
    expect(screen.getByText("You'll be the owner. You can invite others later.")).toBeTruthy();
    const email = screen.getByLabelText('Email address') as HTMLInputElement;
    expect(email.value).toBe('neha@example.com');
    expect(email.readOnly).toBe(true);
    expect(screen.getByLabelText('Confirm password')).toBeTruthy();
    const button = screen.getByRole('button', { name: 'Create library' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    type('Library name', 'Neha family');
    type('Password', 'short');
    expect(button.disabled).toBe(true);
    type('Password', 'long-enough-1');
    expect(button.disabled).toBe(true);
    type('Confirm password', 'long-enough-2');
    expect(button.disabled).toBe(true);
    type('Confirm password', 'long-enough-1');
    expect(button.disabled).toBe(false);
    await act(async () => { fireEvent.click(button); });
    expect(mocks.accept).toHaveBeenCalledWith('t', 'long-enough-1', 'Neha family');
});

it('joins an existing library without a library name', async () => {
    mocks.info.mockResolvedValue({ valid: true, email: 'bob@example.com', targetType: 'join', libraryName: 'Verma family', needsPassword: true });
    mocks.accept.mockResolvedValue({ activeLibraryId: 'x', newAccount: true });
    await mount();
    expect(screen.getByRole('heading', { name: 'Join Verma family' })).toBeTruthy();
    expect(screen.queryByLabelText('Library name')).toBeNull();
    type('Password', 'long-enough-1');
    type('Confirm password', 'long-enough-1');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Accept invite' })); });
    expect(mocks.accept).toHaveBeenCalledWith('t', 'long-enough-1', undefined);
});

it('keeps the existing-account variant: no password, accept while signed in', async () => {
    mocks.info.mockResolvedValue({ valid: true, email: 'bob@example.com', targetType: 'join', accountExists: true, needsPassword: false });
    mocks.accept.mockResolvedValue({ activeLibraryId: 'x', newAccount: false });
    await mount();
    expect(screen.queryByLabelText('Password')).toBeNull();
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Accept invite' })); });
    expect(mocks.accept).toHaveBeenCalledWith('t', undefined, undefined);
});

it('says an invalid or missing invitation no longer works', async () => {
    mocks.info.mockResolvedValue({ valid: false });
    await mount();
    expect(screen.getByRole('heading', { name: 'This link no longer works' })).toBeTruthy();
});
