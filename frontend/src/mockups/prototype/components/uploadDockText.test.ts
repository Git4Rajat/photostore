import { describe, expect, it } from 'vitest';
import { plainFailureReason, timeLeftLabel } from './uploadDockText';

describe('plainFailureReason', () => {
    it('puts known failures in everyday words', () => {
        expect(plainFailureReason('Network error')).toBe('The connection dropped');
        expect(plainFailureReason('HTTP 503')).toBe('The server was busy');
        expect(plainFailureReason('Could not read file')).toBe('This file couldn’t be read');
        expect(plainFailureReason(undefined)).toBe('Something went wrong');
    });
    it('keeps a short unknown message and hides a long one', () => {
        expect(plainFailureReason('Quota full')).toBe('Quota full');
        expect(plainFailureReason('x'.repeat(200))).toBe('Something went wrong');
    });
});

describe('timeLeftLabel', () => {
    it('waits for enough progress before guessing', () => {
        expect(timeLeftLabel(2000, 5, 100)).toBeNull();
        expect(timeLeftLabel(60000, 0, 100)).toBeNull();
    });
    it('rounds to minutes', () => {
        expect(timeLeftLabel(60000, 140, 704)).toBe('About 5 minutes left');
        expect(timeLeftLabel(60000, 100, 10)).toBe('Less than a minute left');
        expect(timeLeftLabel(60000, 1, 1)).toBe('About 1 minute left');
    });
});
