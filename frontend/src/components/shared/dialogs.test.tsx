import React from 'react';
import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DialogHost, confirmDialog, promptDialog } from './dialogs';

describe('DialogHost', () => {
    it('resolves true when the confirm button is clicked', async () => {
        render(<DialogHost />);
        const result = confirmDialog({ title: 'Delete photos', message: 'Delete 3 photos?', confirmLabel: 'Delete', danger: true });
        await screen.findByText('Delete 3 photos?');
        expect(screen.getByText('Delete photos')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
        await expect(result).resolves.toBe(true);
        await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    });

    it('resolves false on cancel', async () => {
        render(<DialogHost />);
        const result = confirmDialog({ message: 'Sure?' });
        await screen.findByText('Sure?');
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        await expect(result).resolves.toBe(false);
    });

    it('resolves the typed value when a prompt is submitted', async () => {
        render(<DialogHost />);
        const result = promptDialog({ title: 'Create album', label: 'Album name', defaultValue: 'Album 1', confirmLabel: 'Create' });
        const input = await screen.findByLabelText('Album name');
        fireEvent.change(input, { target: { value: 'Summer 2026' } });
        fireEvent.click(screen.getByRole('button', { name: 'Create' }));
        await expect(result).resolves.toBe('Summer 2026');
    });

    it('resolves null when a prompt is dismissed with Escape', async () => {
        render(<DialogHost />);
        const result = promptDialog({ label: 'Album name' });
        await screen.findByLabelText('Album name');
        fireEvent.keyDown(window, { key: 'Escape' });
        await expect(result).resolves.toBeNull();
    });

    it('queues a second dialog until the first settles', async () => {
        render(<DialogHost />);
        const first = confirmDialog({ message: 'First question?' });
        const second = confirmDialog({ message: 'Second question?' });
        await screen.findByText('First question?');
        expect(screen.queryByText('Second question?')).toBeNull();
        fireEvent.click(screen.getByRole('button', { name: 'OK' }));
        await expect(first).resolves.toBe(true);
        await screen.findByText('Second question?');
        fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        await expect(second).resolves.toBe(false);
    });
});
