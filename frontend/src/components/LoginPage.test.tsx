import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ login: vi.fn(), forgot: vi.fn(), hint: vi.fn(() => '') }));
vi.mock('../config/appConfig', () => ({ getRuntimeConfig: () => ({ authMode: 'password' }) }));
vi.mock('../services/apiClient', () => ({ setUserId: vi.fn() }));
vi.mock('../services/passwordAuthClient', () => ({
    SIGN_IN_FAILED_MESSAGE: "That email or password isn't right. Try again, or reset your password.",
    getEmailHint: mocks.hint,
    login: mocks.login,
    requestPasswordReset: mocks.forgot,
}));
import LoginPage from './LoginPage';

const mount = () => render(
    <MemoryRouter>
        <LoginPage authEnabled authReady displayName="" onSignIn={async () => {}} />
    </MemoryRouter>,
);
const type = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

beforeEach(() => {
    mocks.login.mockReset();
    mocks.forgot.mockReset().mockResolvedValue(undefined);
    mocks.hint.mockReturnValue('');
});
afterEach(cleanup);

it('draws the mock-up sign-in: brand, labels above the fields, forgot beside the password', () => {
    mount();
    expect(screen.getByRole('heading', { name: 'Welcome back' })).toBeTruthy();
    expect(screen.getByText('Sign in to your photos and videos.')).toBeTruthy();
    expect(screen.getByLabelText('Email address')).toBeTruthy();
    expect(screen.getByLabelText('Password')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Forgot password?' })).toBeTruthy();
    expect(screen.queryByText('Back to gallery')).toBeNull();
    expect(screen.queryByText('ACCESS')).toBeNull();
    expect((screen.getByRole('button', { name: 'Sign in' }) as HTMLButtonElement).disabled).toBe(true);
});

it('shows what was typed when the eye is toggled', () => {
    mount();
    type('Password', 'hunter2hunter2');
    expect((screen.getByLabelText('Password') as HTMLInputElement).type).toBe('password');
    fireEvent.click(screen.getByRole('button', { name: 'Show password' }));
    expect((screen.getByLabelText('Password') as HTMLInputElement).type).toBe('text');
    fireEvent.click(screen.getByRole('button', { name: 'Hide password' }));
    expect((screen.getByLabelText('Password') as HTMLInputElement).type).toBe('password');
});

it('names the problem, marks both fields and keeps what was typed after a failed sign-in', async () => {
    mocks.login.mockRejectedValue(new Error("That email or password isn't right. Try again, or reset your password."));
    mount();
    type('Email address', 'rajat@example.com');
    type('Password', 'wrong-password');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Sign in' })); });
    expect(screen.getByRole('alert').textContent).toContain("That email or password isn't right. Try again, or reset your password.");
    expect(screen.getByLabelText('Email address').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByLabelText('Password').getAttribute('aria-invalid')).toBe('true');
    expect((screen.getByLabelText('Email address') as HTMLInputElement).value).toBe('rajat@example.com');
    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('wrong-password');
});

it('asks for the email before sending a reset, marking only that field', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: 'Forgot password?' }));
    expect(mocks.forgot).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('Enter your email address first');
    expect(screen.getByLabelText('Email address').getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByLabelText('Password').getAttribute('aria-invalid')).toBeNull();
});

it('confirms a reset in the same words whatever the outcome, with a way back', async () => {
    mocks.forgot.mockRejectedValue(new Error('nope'));
    mount();
    type('Email address', 'rajat@example.com');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Forgot password?' })).valueOf(); });
    expect(screen.getByRole('heading', { name: 'Check your email' })).toBeTruthy();
    expect(screen.getByText(/If rajat@example.com has an account, we've sent a link/)).toBeTruthy();
    expect(screen.getByText(/It works for one hour/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Back to sign in' }));
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy();
    expect((screen.getByLabelText('Email address') as HTMLInputElement).value).toBe('rajat@example.com');
});
