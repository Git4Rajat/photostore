import React, { useState } from 'react';
import { CopyGlyph as CopyIcon, LinkGlyph as LinkIcon, LockGlyph as LockIcon, RefreshGlyph as RefreshIcon } from './RedesignIcons';
import { BottomSheet } from './BottomSheet';
import { useStore } from '../store';
import { plural } from '../../../utils/format';
import { confirmDialog } from '../../../components/shared/dialogs';
import type { Album } from '../types';

const EXPIRY_OPTIONS: { label: string; days: number }[] = [
    { label: '1 day', days: 1 },
    { label: '7 days', days: 7 },
    { label: '30 days', days: 30 },
    { label: 'Never', days: 0 },
];
const NEW_LINK_EXPIRY_DAYS = 7;
const LONG_DATE = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'long', year: 'numeric' });

// Must stay at or above the server's MIN_ALBUM_ACCESS_CODE_LENGTH (6), or the share request is rejected.
const randomCode = () => {
    const bytes = new Uint8Array(4);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('').toUpperCase();
};

/** One line saying who can see the album and until when, shown under its actions. */
export const sharingStatus = (album: Album): string | null => {
    if (!album.isPublic) return null;
    const ends = album.publicExpiresAt
        ? ` · ends ${new Date(album.publicExpiresAt).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })}`
        : '';
    return `Anyone with the link${album.hasAccessCode ? ' and the code' : ''} can view${ends}`;
};

const Switch: React.FC<{ on: boolean; label: string; disabled?: boolean; onChange: (on: boolean) => void }> = ({ on, label, disabled, onChange }) => (
    <button type="button" role="switch" aria-checked={on} aria-label={label} className={`pt-switch${on ? ' on' : ''}`} disabled={disabled} onClick={() => onChange(!on)}>
        <span aria-hidden="true" />
    </button>
);

/**
 * Share an album: the sheet itself is the deliberate step, so it says exactly
 * what a viewer sees, when the link stops working and whether it needs a code.
 * Replaces the confirm dialogs and the inline sharing panel.
 */
export const ShareAlbumSheet: React.FC<{
    album: Album;
    open: boolean;
    onClose: () => void;
    /** Codes made in this session, by album id: the server never sends one back. */
    codes: Record<string, string>;
    onCode: (albumId: string, code: string | null) => void;
}> = ({ album, open, onClose, codes, onCode }) => {
    const { shareAlbum, revokeAlbum } = useStore();
    const [busy, setBusy] = useState(false);
    const [copied, setCopied] = useState(false);
    // The expiry picked here until the server confirms it.
    const [pickedDays, setPickedDays] = useState<number | null>(null);
    const { id } = album;
    const code = codes[id];
    const url = album.publicUrl ?? '';
    const run = async (task: () => Promise<unknown>) => {
        setBusy(true);
        try { await task(); } finally { setBusy(false); }
    };

    const setLink = (on: boolean) => void run(async () => {
        if (on) await shareAlbum(id, { expiresInDays: NEW_LINK_EXPIRY_DAYS });
        else { await revokeAlbum(id); onCode(id, null); }
    });
    const pickExpiry = (days: number) => {
        setPickedDays(days);
        void run(() => shareAlbum(id, { expiresInDays: days })).finally(() => setPickedDays(null));
    };
    const setCodeRequired = (on: boolean) => void run(async () => {
        if (on) {
            const next = randomCode();
            if (await shareAlbum(id, { accessCode: next })) onCode(id, next);
            return;
        }
        // Dropping the code opens the album to anyone with the link, so it asks first.
        const ok = await confirmDialog({
            title: `Remove the access code from “${album.name}”?`,
            message: 'Anyone with the link will be able to see the photos without a code.',
            confirmLabel: 'Remove code',
            danger: true,
        });
        if (ok && await shareAlbum(id, { clearAccessCode: true })) onCode(id, null);
    });
    const newCode = () => void run(async () => {
        const next = randomCode();
        if (await shareAlbum(id, { accessCode: next })) onCode(id, next);
    });
    const copy = () => {
        if (!url) return;
        void navigator.clipboard?.writeText(url).catch(() => {});
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
    };
    const expiresOn = album.publicExpiresAt ? new Date(album.publicExpiresAt) : null;
    // Highlight the option the link was set with: the one nearest its days left.
    const daysLeft = expiresOn ? (expiresOn.getTime() - Date.now()) / 86_400_000 : 0;
    const activeDays = pickedDays ?? (expiresOn
        ? EXPIRY_OPTIONS.filter((o) => o.days > 0).reduce((best, o) => (Math.abs(o.days - daysLeft) < Math.abs(best - daysLeft) ? o.days : best), 1)
        : 0);
    const codeOn = Boolean(album.hasAccessCode || code);

    return (
        <BottomSheet open={open} onClose={onClose} title={`Share “${album.name}”`}>
            <div className="pt-share">
                <div className="pt-share-row">
                    <LinkIcon className="pt-share-icon accent" aria-hidden="true" />
                    <span className="pt-share-text">
                        <b>Link sharing</b>
                        <small>Anyone with the link can see {album.photoCount === 1 ? 'this item' : `these ${plural(album.photoCount, 'item')}`}, and nothing else in your library.</small>
                    </span>
                    <Switch on={Boolean(album.isPublic)} label="Link sharing" disabled={busy} onChange={setLink} />
                </div>

                {album.isPublic && (
                    <>
                        {url && (
                            <div className="pt-share-url">
                                <span className="share-url">{url}</span>
                                <button type="button" className="btn btn-sm" onClick={copy}><CopyIcon className="toolbar-icon" />{copied ? 'Copied' : 'Copy'}</button>
                            </div>
                        )}
                        <div className="pt-share-expiry">
                            <span className="pt-share-label">Link expires</span>
                            <div className="mock-seg" role="group" aria-label="Link expires">
                                {EXPIRY_OPTIONS.map((o) => (
                                    <button key={o.label} type="button" disabled={busy} className={activeDays === o.days ? 'active' : undefined} aria-pressed={activeDays === o.days} onClick={() => pickExpiry(o.days)}>
                                        {o.label}
                                    </button>
                                ))}
                            </div>
                            <span className="pt-share-note">{expiresOn ? `Stops working on ${LONG_DATE.format(expiresOn)}` : 'Never stops working'}</span>
                        </div>
                        <div className="pt-share-row">
                            <LockIcon className="pt-share-icon" aria-hidden="true" />
                            <span className="pt-share-text">
                                <b>Ask for a code</b>
                                {code
                                    ? <small>Give viewers this code: <code className="pt-access-code">{code}</code></small>
                                    : <small>{album.hasAccessCode ? 'Viewers need the code you gave them. Make a new one to see it here.' : 'Viewers type a code you give them. Protected links open a little slower.'}</small>}
                            </span>
                            <Switch on={codeOn} label="Ask for a code" disabled={busy} onChange={setCodeRequired} />
                        </div>
                        {codeOn && (
                            <button type="button" className="pt-linkish pt-share-newcode" disabled={busy} onClick={newCode}>
                                <RefreshIcon className="toolbar-icon" /> Make a new code
                            </button>
                        )}
                    </>
                )}

                <button type="button" className="btn btn-primary pt-share-done" onClick={onClose}>Done</button>
                {album.isPublic && (
                    <div className="pt-share-stop">
                        <button type="button" className="pt-linkish danger" disabled={busy} onClick={() => setLink(false)}>Stop sharing</button>
                        <small>Anyone with the link loses access straight away.</small>
                    </div>
                )}
            </div>
        </BottomSheet>
    );
};

export default ShareAlbumSheet;
