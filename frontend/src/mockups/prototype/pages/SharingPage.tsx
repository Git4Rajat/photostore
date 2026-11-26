import React, { useEffect, useState } from 'react';
import { Send as SendIcon } from 'lucide-react';
import { ClockGlyph as ClockIcon, MailGlyph as MailIcon, PencilGlyph as PencilIcon, UserPlusGlyph as UserPlusIcon } from '../components/RedesignIcons';
import { BottomSheet } from '../components/BottomSheet';
import { useStore } from '../store';
import { Avatar, Spinner } from '../components/bits';
import { initialsFor, plural } from '../../../utils/format';
import * as library from '../../../services/libraryClient';
import { getRuntimeConfig } from '../../../config/appConfig';
import { confirmDialog, promptDialog } from '../../../components/shared/dialogs';
import DownloadLibrary from '../components/DownloadLibrary';
import DeleteLibraryDialog from '../components/DeleteLibraryDialog';
import { cleanSendNotice } from '../components/cleanSendNotice';
import { publishNotification } from '../../../services/notificationSink';
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
        members, pendingInvites, receivedInvites, libraryName, isOwner, maxMembers, membersLoading,
        fetchMembers, invite, revokeInvite, removeMember, renameLibrary, toast, totalPhotos, albums,
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
    // Handing the library over makes the other person its owner; the caller stays on as a member.
    const makeOwner = async (userId: string, label: string) => {
        const ok = await confirmDialog({
            title: `Make ${label} the owner?`,
            message: 'They will control this library, including inviting, removing people and deleting photos. You stay on as a member and get a new library of your own.',
            confirmLabel: 'Make owner',
            danger: true,
        });
        if (!ok) return;
        try {
            if (passwordMode) {
                const password = await promptDialog({ title: 'Confirm your password', label: 'Password', type: 'password', confirmLabel: 'Continue' });
                if (!password) return;
                await library.transferOwnership(userId, { proof: await library.verifyLibraryPassword(password) });
            } else {
                const typed = await promptDialog({ title: 'Confirm new owner', label: `Type ${label} to confirm`, confirmLabel: 'Make owner' });
                if (!typed) return;
                await library.transferOwnership(userId, { confirmEmail: typed });
            }
            window.location.assign('/');
        } catch (err) {
            toast(err instanceof Error ? err.message : 'Couldn’t hand over the library', undefined, undefined, 'error');
        }
    };
    // Invitations other libraries sent to this account. Accepting changes the active library, so reload.
    const answerInvite = async (invite: library.ReceivedInvite, accept: boolean) => {
        try {
            if (accept) {
                await library.acceptReceivedInvite(invite);
                window.location.assign('/');
            } else {
                await library.declineReceivedInvite(invite);
                toast('Invitation declined');
                void fetchMembers();
            }
        } catch (err) {
            toast(err instanceof Error ? err.message : 'Couldn’t answer that invitation', undefined, undefined, 'error');
            void fetchMembers();
        }
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

    // "Delete everything": the dialog checks the password and starts the email; this
    // reports how the send went once the owner has pressed OK.
    const [cleanOpen, setCleanOpen] = useState(false);
    const [cleanStart, setCleanStart] = useState<'warn' | 'password'>('warn');
    const ownerEmail = members.find((m) => m.isSelf)?.email || '';
    const otherMembers = members.filter((m) => !m.isSelf).map((m) => m.email || m.userId);
    // The result lands in Notifications (a toast only where there is no panel).
    const reportCleanSend = (outcome: Promise<library.CleanRequestResult>, proof?: string) => {
        const who = otherMembers.length === 1 ? otherMembers[0] : 'Another member';
        const publish = (result: library.CleanRequestResult | unknown) => {
            const notice = cleanSendNotice(result, who);
            const action = notice.action === 'retry'
                ? { label: 'Try again', onClick: () => reportCleanSend(library.requestLibraryClean(proof), proof) }
                : notice.action === 'reauth'
                    ? { label: 'Confirm', onClick: () => { setCleanStart('password'); setCleanOpen(true); } }
                    : undefined;
            if (publishNotification(notice.title, notice.details, action)) return;
            toast(notice.details ? `${notice.title}. ${notice.details}` : notice.title, action?.label, action?.onClick, notice.action ? 'error' : 'info');
        };
        void outcome.then(publish, publish);
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

                    {receivedInvites.length > 0 && (
                        <>
                            <h2 className="pt-section-label">Invitations for you</h2>
                            <div className="pt-list">
                                {receivedInvites.map((r) => (
                                    <div key={`${r.libraryId}:${r.inviteId}`} className="member-row pending">
                                        <span className="pt-invite-icon" aria-hidden="true"><MailIcon /></span>
                                        <span className="member-meta">
                                            <b>{r.libraryName || 'A shared library'}</b>
                                            <span>{r.invitedBy ? `From ${r.invitedBy} · ` : ''}{inviteEnds(r.expiresAt)}</span>
                                        </span>
                                        <button type="button" className="mock-link" onClick={() => void answerInvite(r, true)}>Join</button>
                                        <button type="button" className="mock-link muted" onClick={() => void answerInvite(r, false)}>Decline</button>
                                    </div>
                                ))}
                            </div>
                        </>
                    )}

                    <h2 className="pt-section-label">Members</h2>
                    <div className="pt-list">
                        {membersLoading && members.length === 0 && <Spinner label="Loading members…" center={false} />}
                        {members.map((m) => (
                            <div key={m.userId} className="member-row">
                                <Avatar initials={initialsFor(m.email || m.userId)} />
                                <span className="member-meta"><b>{m.email || m.userId}</b><span>{m.isSelf ? 'You' : 'Member'}</span></span>
                                {m.isOwner && <span className="member-badge owner">Owner</span>}
                                {isOwner && !m.isOwner && !m.isSelf && (
                                    <>
                                        <button type="button" className="mock-link muted" onClick={() => void makeOwner(m.userId, m.email || m.userId)}>Make owner</button>
                                        <button type="button" className="mock-link muted" onClick={() => void confirmRemove(m.userId, m.email || m.userId)}>Remove</button>
                                    </>
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
                            <button type="button" className="pt-linkish danger" onClick={() => { setCleanStart('warn'); setCleanOpen(true); }}>Delete everything in this library…</button>
                            <small>Removes every photo for every member. Asks for {passwordMode ? 'your password and ' : ''}an email confirmation first.</small>
                        </div>
                    )}
                </div>

                {/* Desktops keep the invite form beside the list; phones open it as a sheet. */}
                {isOwner && <aside className="pt-library-invite card-glass" aria-label="Invite someone">{inviteForm}</aside>}
            </div>

            <DeleteLibraryDialog
                open={cleanOpen}
                startAt={cleanStart}
                onClose={() => setCleanOpen(false)}
                libraryName={libraryName}
                totalPhotos={totalPhotos}
                albumCount={albums.length}
                passwordMode={passwordMode}
                ownerEmail={ownerEmail}
                otherMembers={otherMembers}
                onSendStarted={reportCleanSend}
            />

            <BottomSheet open={inviteOpen} onClose={() => setInviteOpen(false)} title="Invite someone">
                {inviteForm}
            </BottomSheet>
        </div>
    );
};

export default SharingPage;
