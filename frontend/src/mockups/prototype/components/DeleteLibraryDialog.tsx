import React, { useCallback, useEffect, useRef, useState } from 'react';
import { BottomSheet } from './BottomSheet';
import { AlertGlyph, EyeGlyph, MailGlyph } from './RedesignIcons';
import * as library from '../../../services/libraryClient';

type Step = 'warn' | 'password' | 'inbox';

// CLEAN_REQUEST_TTL_SECONDS in backend/library_utils.py
const LINK_MINUTES = 30;

/** `j***@example.com`, the same shape the server uses for the addresses it reports. */
export const maskEmail = (address: string): string => {
    const [local, domain] = address.split('@');
    return domain ? `${local.slice(0, 1)}***@${domain}` : '';
};

interface Props {
    open: boolean;
    /** Where to begin: 'password' reopens at the step that failed after a proof ran out. */
    startAt?: 'warn' | 'password';
    onClose: () => void;
    libraryName: string;
    totalPhotos: number | null;
    albumCount: number;
    passwordMode: boolean;
    ownerEmail: string;
    /** Emails of the other members; one of them is asked to confirm too. */
    otherMembers: string[];
    /** The dialog was dismissed after the email was started: report how the send went. */
    onSendStarted: (outcome: Promise<library.CleanRequestResult>, proof?: string) => void;
}

/**
 * "Delete everything in this library": warning, password, then "check your inbox".
 * The password is checked here, on core, so a wrong one is answered inside the
 * dialog at once. The email goes out in the background; once the owner presses
 * OK the parent reports whether it was sent.
 */
export const DeleteLibraryDialog: React.FC<Props> = ({
    open, startAt = 'warn', onClose, libraryName, totalPhotos, albumCount, passwordMode, ownerEmail, otherMembers, onSendStarted,
}) => {
    const [step, setStep] = useState<Step>('warn');
    const [password, setPassword] = useState('');
    const [reveal, setReveal] = useState(false);
    const [checking, setChecking] = useState(false);
    const [error, setError] = useState('');
    const stepRef = useRef(step);
    const checkingRef = useRef(false);
    const sendRef = useRef<{ outcome: Promise<library.CleanRequestResult>; proof?: string } | null>(null);
    const latest = useRef({ onClose, onSendStarted });
    latest.current = { onClose, onSendStarted };
    const inputRef = useRef<HTMLInputElement | null>(null);
    const okRef = useRef<HTMLButtonElement | null>(null);

    const go = (next: Step) => { stepRef.current = next; setStep(next); };

    useEffect(() => {
        if (!open) return;
        go(startAt);
        setPassword(''); setReveal(false); setError(''); setChecking(false);
        checkingRef.current = false;
        sendRef.current = null;
    }, [open, startAt]);

    useEffect(() => {
        if (!open) return;
        if (step === 'password') inputRef.current?.focus();
        if (step === 'inbox') okRef.current?.focus();
    }, [open, step]);

    // Stable: BottomSheet re-focuses its first control whenever this changes.
    const close = useCallback(() => {
        if (checkingRef.current) return;
        if (stepRef.current === 'inbox' && sendRef.current) {
            latest.current.onSendStarted(sendRef.current.outcome, sendRef.current.proof);
            sendRef.current = null;
        }
        latest.current.onClose();
    }, []);

    const startSend = (proof?: string) => {
        const outcome = library.requestLibraryClean(proof);
        outcome.catch(() => { /* reported by the parent once the owner presses OK */ });
        sendRef.current = { outcome, proof };
        go('inbox');
    };

    const onContinue = () => {
        if (passwordMode) go('password');
        else startSend();
    };

    const submitPassword = async (event: React.FormEvent) => {
        event.preventDefault();
        if (checkingRef.current || !password) return;
        checkingRef.current = true;
        setChecking(true);
        setError('');
        try {
            const proof = await library.verifyLibraryPassword(password);
            startSend(proof);
        } catch (err) {
            setError(err instanceof library.WrongPasswordError
                ? 'That password isn’t right. Try again. Nothing has been sent.'
                : `${err instanceof Error ? err.message : 'Could not check the password.'} Nothing has been sent.`);
            window.setTimeout(() => inputRef.current?.focus(), 0);
        } finally {
            checkingRef.current = false;
            setChecking(false);
        }
    };

    const shared = otherMembers.length > 0;
    const who = otherMembers.length === 1 ? otherMembers[0] : 'Another member';
    const owner = maskEmail(ownerEmail);
    const title = step === 'warn' ? 'Delete everything in this library?'
        : step === 'password' ? 'Confirm with your password'
            : 'Check your inbox, and spam';
    const what = [
        totalPhotos !== null ? `all ${totalPhotos} photos and videos` : 'every photo and video',
        albumCount > 0 ? `${albumCount} ${albumCount === 1 ? 'album' : 'albums'}` : '',
        'everyone’s access',
    ].filter(Boolean);

    return (
        <BottomSheet open={open} onClose={close} title={title}>
            <div className="pt-clean">
                {step === 'warn' && (
                    <>
                        <span className="pt-clean-icon bad" aria-hidden="true"><AlertGlyph /></span>
                        <p>
                            This permanently deletes {what.length > 2 ? `${what.slice(0, -1).join(', ')} and ${what[what.length - 1]}` : what.join(' and ')} to{' '}
                            <b>{libraryName || 'this library'}</b>. It can’t be undone.
                        </p>
                        {shared && <p>This library is shared. {who} will be asked to confirm too.</p>}
                        <button type="button" className="btn btn-danger" onClick={onContinue}>Continue</button>
                        <button type="button" className="btn" onClick={close}>Cancel</button>
                    </>
                )}
                {step === 'password' && (
                    <form className="pt-clean" onSubmit={(e) => void submitPassword(e)}>
                        <p>We’ll then email you a link to confirm. Nothing is deleted yet.</p>
                        <div className="pt-clean-fg">
                            <label htmlFor="clean-password">Your password</label>
                            <div className="pt-clean-field">
                                <input
                                    id="clean-password"
                                    ref={inputRef}
                                    className="field"
                                    type={reveal ? 'text' : 'password'}
                                    autoComplete="current-password"
                                    value={password}
                                    disabled={checking}
                                    aria-invalid={error ? true : undefined}
                                    aria-describedby={error ? 'clean-password-error' : undefined}
                                    onChange={(e) => { setPassword(e.target.value); if (error) setError(''); }}
                                />
                                <button type="button" className="pt-clean-eye" aria-label={reveal ? 'Hide password' : 'Show password'} aria-pressed={reveal} disabled={checking} onClick={() => setReveal((v) => !v)}>
                                    <EyeGlyph aria-hidden="true" />
                                </button>
                            </div>
                            {error && <p id="clean-password-error" className="pt-clean-error" role="alert"><AlertGlyph aria-hidden="true" /><span>{error}</span></p>}
                        </div>
                        <button type="submit" className="btn btn-danger" disabled={checking || !password}>
                            {checking ? <><span className="pt-spinner" aria-hidden="true" />Checking…</> : 'Confirm'}
                        </button>
                        <button type="button" className="btn" disabled={checking} onClick={close}>Cancel</button>
                    </form>
                )}
                {step === 'inbox' && (
                    <>
                        <span className="pt-clean-icon" aria-hidden="true"><MailGlyph /></span>
                        <p>
                            We’re sending a confirmation link to <b>{owner || 'your email address'}</b>.
                            {shared && ` Because this library is shared, ${who} will be asked to confirm too.`}
                        </p>
                        <p>
                            {shared ? `Both links expire in ${LINK_MINUTES} minutes. Nothing is deleted until you both open them.`
                                : `The link expires in ${LINK_MINUTES} minutes. Nothing is deleted until you open it.`}
                        </p>
                        <button ref={okRef} type="button" className="btn btn-primary" onClick={close}>OK</button>
                    </>
                )}
            </div>
        </BottomSheet>
    );
};

export default DeleteLibraryDialog;
