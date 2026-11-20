import * as library from '../../../services/libraryClient';

// CLEAN_REQUEST_TTL_SECONDS in backend/library_utils.py
const LINK_MINUTES = 30;

export interface CleanSendNotice {
    title: string;
    details: string;
    /** What the entry's button does: send the email again, or ask for the password again. */
    action?: 'retry' | 'reauth';
}

/** The Notifications entry for how the confirmation email went (outcome: a result, or why it failed). */
export const cleanSendNotice = (outcome: library.CleanRequestResult | unknown, who: string): CleanSendNotice => {
    if (outcome && typeof outcome === 'object' && 'sentTo' in outcome) {
        const result = outcome as library.CleanRequestResult;
        const asked = result.requiresAdditionalApproval ? `${who} has been asked to confirm too. ` : '';
        return {
            title: `Confirmation link sent to ${result.sentTo?.[0] || 'your email address'}`,
            details: `${asked}The link works for ${LINK_MINUTES} minutes. Nothing is deleted until you open it.`,
        };
    }
    if (outcome instanceof library.CleanRequestError && outcome.code === 'reauth_required') {
        return { title: 'Please confirm your password again.', details: 'Nothing was deleted.', action: 'reauth' };
    }
    if (outcome instanceof library.CleanRequestError && outcome.status !== 502) {
        return { title: outcome.message, details: '' };
    }
    return { title: 'We couldn’t send the email. Nothing was deleted.', details: '', action: 'retry' };
};
