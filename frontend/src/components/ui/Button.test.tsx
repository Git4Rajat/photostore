import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { Button, Chip, IconButton } from './Button';

describe('Button', () => {
    it('defaults to a plain, non-submitting button', () => {
        render(<Button>Cancel</Button>);
        const button = screen.getByRole('button', { name: 'Cancel' });
        expect(button.className).toBe('btn');
        expect(button.getAttribute('type')).toBe('button');
    });

    it('maps variant and size onto the shared classes', () => {
        render(<Button variant="danger" size="sm" className="extra">Delete</Button>);
        expect(screen.getByRole('button', { name: 'Delete' }).className).toBe('btn btn-danger btn-sm extra');
    });

    it('can submit a form when asked to', () => {
        render(<Button type="submit" variant="primary">Save</Button>);
        expect(screen.getByRole('button', { name: 'Save' }).getAttribute('type')).toBe('submit');
    });
});

describe('IconButton', () => {
    it('names itself from its label', () => {
        render(<IconButton label="Close"><svg /></IconButton>);
        const button = screen.getByRole('button', { name: 'Close' });
        expect(button.getAttribute('title')).toBe('Close');
        expect(button.className).toBe('btn btn-icon btn-soft');
    });
});

describe('Chip', () => {
    it('reports whether it is selected and handles clicks', () => {
        const onClick = vi.fn();
        const { rerender } = render(<Chip onClick={onClick}>Videos</Chip>);
        const chip = screen.getByRole('button', { name: 'Videos' });
        expect(chip.getAttribute('aria-pressed')).toBe('false');
        fireEvent.click(chip);
        expect(onClick).toHaveBeenCalledTimes(1);
        rerender(<Chip selected onClick={onClick}>Videos</Chip>);
        expect(chip.getAttribute('aria-pressed')).toBe('true');
    });
});
