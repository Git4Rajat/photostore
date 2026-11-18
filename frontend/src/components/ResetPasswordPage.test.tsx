import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ reset: vi.fn() }));
vi.mock('../services/passwordAuthClient', () => ({ resetPassword: mocks.reset }));
import ResetPasswordPage from './ResetPasswordPage';

const mount = (url: string) => render(<MemoryRouter initialEntries={[url]}><ResetPasswordPage /></MemoryRouter>);
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
afterEach(() => { cleanup(); mocks.reset.mockReset(); });

it('says a link without a token no longer works and offers the way back', () => {
    mount('/reset-password');
    expect(screen.getByRole('heading', { name: 'This link no longer works' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Back to sign in' })).toBeTruthy();
});

it('keeps the button dimmed until the rules are met, then resets', async () => {
    mocks.reset.mockResolvedValue(undefined);
    mount('/reset-password?token=t');
    const button = screen.getByRole('button', { name: 'Reset password' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    type('New password', 'longenough1');
    expect(button.disabled).toBe(true);
    type('Confirm new password', 'longenough1');
    expect(button.disabled).toBe(false);
    await act(async () => { fireEvent.click(button); });
    expect(mocks.reset).toHaveBeenCalledWith('t', 'longenough1');
    expect(screen.getByText(/Your password has been reset/)).toBeTruthy();
});

it('switches to the ended-link page when the server refuses the token', async () => {
    mocks.reset.mockRejectedValue(new Error('This reset link is invalid or has expired. Please request a new one.'));
    mount('/reset-password?token=old');
    type('New password', 'longenough1');
    type('Confirm new password', 'longenough1');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Reset password' })); });
    expect(screen.getByRole('heading', { name: 'This link no longer works' })).toBeTruthy();
});
