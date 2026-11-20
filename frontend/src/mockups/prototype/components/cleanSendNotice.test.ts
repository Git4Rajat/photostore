import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../services/libraryClient', () => {
    class CleanRequestError extends Error {
        constructor(message: string, readonly status: number, readonly code?: string) { super(message); }
    }
    return { CleanRequestError };
});
import * as library from '../../../services/libraryClient';
import { cleanSendNotice } from './cleanSendNotice';

const sent = (extra = {}) => ({ status: 'pending', requiresAdditionalApproval: false, sentTo: ['r***@example.test'], expiresIn: 1800, ...extra });

describe('how the confirmation email went, as a Notifications entry', () => {
    it('says where the link went, how long it works, and that nothing is deleted yet', () => {
        expect(cleanSendNotice(sent(), 'Asha')).toEqual({
            title: 'Confirmation link sent to r***@example.test',
            details: 'The link works for 30 minutes. Nothing is deleted until you open it.',
        });
    });

    it('adds that the other member has been asked when the library is shared', () => {
        expect(cleanSendNotice(sent({ requiresAdditionalApproval: true }), 'asha@example.test').details)
            .toBe('asha@example.test has been asked to confirm too. The link works for 30 minutes. Nothing is deleted until you open it.');
    });

    it('offers Try again when the send failed, including when the server was unreachable', () => {
        const failed = { title: 'We couldn’t send the email. Nothing was deleted.', details: '', action: 'retry' };
        expect(cleanSendNotice(new library.CleanRequestError('Could not send', 502), 'Asha')).toEqual(failed);
        expect(cleanSendNotice(new TypeError('Failed to fetch'), 'Asha')).toEqual(failed);
    });

    it('asks for the password again when the proof has run out', () => {
        expect(cleanSendNotice(new library.CleanRequestError('x', 401, 'reauth_required'), 'Asha').action).toBe('reauth');
    });

    it('passes on the server’s reason when retrying cannot help', () => {
        expect(cleanSendNotice(new library.CleanRequestError('A cleanup confirmation is already pending for this library.', 409), 'Asha'))
            .toEqual({ title: 'A cleanup confirmation is already pending for this library.', details: '' });
    });
});
