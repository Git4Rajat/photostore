import React, { useEffect, useState } from 'react';
import { Clock as ClockIcon, Mail as MailIcon, Pencil as PencilIcon, Send as SendIcon, UserPlus as UserPlusIcon } from 'lucide-react';
import { BottomSheet } from '../components/BottomSheet';
import { useStore } from '../store';
import { Avatar, Spinner } from '../components/bits';
import { initialsFor, plural } from '../../../utils/format';
import * as library from '../../../services/libraryClient';
import { getRuntimeConfig } from '../../../config/appConfig';
import { confirmDialog, promptDialog } from '../../../components/shared/dialogs';
import DownloadLibrary from '../components/DownloadLibrary';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const INVITE_HOURS = 72; // INVITE_TTL_SECONDS in backend/library_utils.py

/** When a pending invite stops working, in words. */
export const inviteEnds = (expiresAt: number, now = Date.now()): string => {
    const ms = (expiresAt < 1e12 ? expiresAt * 1000 : expiresAt) - now;
    if (ms <= 0) return 'ended';
    const hours = Math.ceil(ms / 3_600_000);
    if (hours <= 24) return hours <= 1 ? 'ends within the hour' : `ends in ${hours} hours`;
    return `ends in ${plural(Math.ceil(hours / 24), 'day')}`;
};

/** Email plus the two choices, spelled out instead of a dropdown. */
const InviteForm: React.FC<{
    libraryName: string;
    items: number | null;
    email: string;
    onEmail: (v: string) => void;
    targetType: 'join' | 'fresh';
    onTarget: (v: 'join' | 'fresh') => void;
    valid: boolean;
    atCapacity: boolean;
    onSend: () => void;
}> = ({ libraryName, items, email, onEmail, targetType, onTarget, valid, atCapacity, onSend }) => (
    <form className="pt-invite-form" onSubmit={(e) => { e.preventDefault(); onSend(); }}>
        <h3 className="pt-invite-title">Invite someone</h3>
        <label className="pt-invite-field">
            <span>Email address</span>
            <input className="field" type="email" autoComplete="off" value={email} onChange={(e) => onEmail(e.target.value)} placeholder="name@example.com" disabled={atCapacity} />
        </label>
        <fieldset className="pt-invite-options">
            <legend>What should they get?</legend>
            <label className={`pt-option${targetType === 'join' ? ' on' : ''}`}>
                <input type="radio" name="invite-target" checked={targetType === 'join'} onChange={() => onTarget('join')} />
                <span><b>Join {libraryName}</b><small>They see {items !== null ? `all ${plural(items, 'item')}` : 'everything'} here and can add their own.</small></span>
            </label>
            <label className={`pt-option${targetType === 'fresh' ? ' on' : ''}`}>
                <input type="radio" name="invite-target" checked={targetType === 'fresh'} onChange={() => onTarget('fresh')} />
                <span><b>Their own library</b><small>A separate, empty library. Nothing here is shared with them.</small></span>
            </label>
        </fieldset>
        <p className="pt-invite-note"><ClockIcon aria-hidden="true" />The invite link works once and ends after {INVITE_HOURS} hours.</p>
        <button type="submit" className="btn btn-primary" disabled={!valid || atCapacity}><SendIcon className="toolbar-icon" /> Send invite</button>
    </form>
);

/** Sharing — shared-library members, pending invites, and library settings. */
const SharingPage: React.FC = () => {
    const {
        members, pendingInvites, libraryName, isOwner, maxMembers, membersLoading,
        fetchMembers, invite, revokeInvite, removeMember, renameLibrary, toast, totalPhotos,
    } = useStore();
    const [inviteOpen, setInviteOpen] = useState(false);
    const passwordMode = (getRuntimeConfig().authMode || '').toLowerCase() === 'password';
    // Removing someone takes the whole library away from them, so it asks first.
    const confirmRemove = async (userId: string, label: string) => {
        const ok = await confirmDialog({
            title: `Remove ${label}?`,
            message: 'They lose access to this library straight away. Their uploads stay in it.',
            confirmLabel: 'Remove',
            danger: true,
        });
        if (ok) removeMember(userId);
    };
    const [email, setEmail] = useState('');
    const [targetType, setTargetType] = useState<'join' | 'fresh'>('join');
    const valid = EMAIL_RE.test(email.trim());
    const memberCount = members.length;
    const atCapacity = memberCount + pendingInvites.filter((p) => p.targetType === 'join').length >= maxMembers;

    // Loads members when this tab is actually visited, queued behind
    // whatever else is in flight, aborted if the user navigates away before
    // its turn -- this used to ALSO fire unconditionally from StoreProvider
    // on every app mount (every session paid for a members fetch even if
    // Sharing was never opened), so visiting this tab fetched members twice.
    // See the 2026-10-01 boot-request audit.
    useEffect(() => {
        const controller = new AbortController();
        void enqueueBackgroundRequest(() => fetchMembers(), { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    const send = () => {
        if (!valid || atCapacity) return;
        invite(email.trim(), targetType);
        setEmail('');
    };

    const renameLibraryPrompt = async () => {
        const next = await promptDialog({
            title: 'Rename library',
            label: 'Library name',
            defaultValue: libraryName,
            placeholder: 'e.g. Family photos',
            confirmLabel: 'Rename',
        });
        if (next && next.trim()) renameLibrary(next.trim());
    };

    const cleanLibrary = async () => {
        const confirmed = await confirmDialog({
            title: 'Clean library?',
            message: 'This removes ALL photos and videos from this library. You’ll get an emailed link to confirm before anything is actually deleted.',
            confirmLabel: 'Continue',
            danger: true,
        });
        if (!confirmed) return;
        let password: string | undefined;
        if (passwordMode) {
            const entered = await promptDialog({
                title: 'Confirm it’s you',
                message: 'Re-enter your password to start the cleanup.',
                label: 'Password',
                type: 'password',
                confirmLabel: 'Confirm',
            });
            if (!entered) return;
            password = entered;
        }
        void library.requestLibraryClean(password)
            .then((res) => toast(res.sentTo?.length ? `Confirmation link sent to ${res.sentTo.join(', ')}` : 'Cleanup confirmation requested'))
            .catch((err) => toast(err instanceof Error ? err.message : 'Couldn’t start cleanup', undefined, undefined, 'error'));
    };

    const name = libraryName || 'Your library';
    const itemsText = totalPhotos !== null ? `${plural(totalPhotos, 'item')} · ` : '';
    const inviteForm = (
        <InviteForm
            libraryName={name}
            items={totalPhotos}
            email={email}
            onEmail={setEmail}
            targetType={targetType}
            onTarget={setTargetType}
            valid={valid}
            atCapacity={atCapacity}
            onSend={() => { send(); setInviteOpen(false); }}
        />
    );

    return (
        <div className="pt-library">
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Library and sharing</h1>
                </div>
            </div>

            <div className="pt-library-cols">
                <div className="pt-library-main">
                    <div className="pt-library-name">
                        <span>
                            <b>{name}</b>
                            <small>{itemsText}{memberCount} of {plural(maxMembers, 'member')}</small>
                        </span>
                        {isOwner && (
                            <button type="button" className="btn btn-sm" onClick={() => void renameLibraryPrompt()}>
                                <PencilIcon className="toolbar-icon" /> Rename
                            </button>
                        )}
                    </div>

                    <h2 className="pt-section-label">Members</h2>
                    <div className="pt-list">
                        {membersLoading && members.length === 0 && <Spinner label="Loading members…" center={false} />}
                        {members.map((m) => (
                            <div key={m.userId} className="member-row">
                                <Avatar initials={initialsFor(m.email || m.userId)} />
                                <span className="member-meta"><b>{m.email || m.userId}</b><span>{m.isSelf ? 'You' : 'Member'}</span></span>
                                {m.isOwner && <span className="member-badge owner">Owner</span>}
                                {isOwner && !m.isOwner && !m.isSelf && (
                                    <button type="button" className="mock-link muted" onClick={() => void confirmRemove(m.userId, m.email || m.userId)}>Remove</button>
                                )}
                            </div>
                        ))}
                    </div>

                    {pendingInvites.length > 0 && (
                        <>
                            <h2 className="pt-section-label">Waiting to join</h2>
                            <div className="pt-list">
                                {pendingInvites.map((p) => (
                                    <div key={p.inviteId} className="member-row pending">
                                        <span className="pt-invite-icon" aria-hidden="true"><MailIcon /></span>
                                        <span className="member-meta">
                                            <b>{p.email}</b>
                                            <span>{p.targetType === 'fresh' ? 'Their own library' : `Joining ${name}`} · {inviteEnds(p.expiresAt)}</span>
                                        </span>
                                        {isOwner && <button type="button" className="mock-link muted" onClick={() => revokeInvite(p.inviteId)}>Cancel invite</button>}
                                    </div>
                                ))}
                            </div>
                        </>
                    )}

                    {isOwner ? (
                        <button type="button" className="btn btn-primary pt-invite-open" disabled={atCapacity} onClick={() => setInviteOpen(true)}>
                            <UserPlusIcon className="toolbar-icon" /> Invite someone
                        </button>
                    ) : (
                        <p className="pt-page-sub">Only the library owner can invite or remove members.</p>
                    )}
                    {atCapacity && isOwner && <p className="pt-page-sub">This library is at its member limit.</p>}

                    {isOwner && <DownloadLibrary />}

                    {/* The destructive option sits apart, at the bottom. */}
                    {isOwner && (
                        <div className="pt-library-danger">
                            <button type="button" className="pt-linkish danger" onClick={() => void cleanLibrary()}>Delete everything in this library…</button>
                            <small>Removes every photo for every member. Asks for {passwordMode ? 'your password and ' : ''}an email confirmation first.</small>
                        </div>
                    )}
                </div>

                {/* Desktops keep the invite form beside the list; phones open it as a sheet. */}
                {isOwner && <aside className="pt-library-invite card-glass" aria-label="Invite someone">{inviteForm}</aside>}
            </div>

            <BottomSheet open={inviteOpen} onClose={() => setInviteOpen(false)} title="Invite someone">
                {inviteForm}
            </BottomSheet>
        </div>
    );
};

export default SharingPage;
