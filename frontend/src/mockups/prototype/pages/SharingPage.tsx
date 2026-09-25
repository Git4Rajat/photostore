import React, { useEffect, useState } from 'react';
import { PaperAirplaneIcon } from '@heroicons/react/24/outline';
import { useStore } from '../store';
import { Avatar } from '../components/bits';
import * as library from '../../../services/libraryClient';
import { getRuntimeConfig } from '../../../config/appConfig';
import { confirmDialog, promptDialog } from '../../../components/shared/dialogs';

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const initialsFor = (value: string): string => {
    const parts = value.replace(/@.*/, '').split(/[\s._-]+/).filter(Boolean);
    const letters = parts.length >= 2 ? parts[0][0] + parts[1][0] : value.slice(0, 2);
    return (letters || '?').toUpperCase();
};

/** Sharing — shared-library members, pending invites, and library settings. */
export const SharingPage: React.FC = () => {
    const {
        members, pendingInvites, libraryName, isOwner, maxMembers, membersLoading,
        reloadMembers, invite, revokeInvite, removeMember, renameLibrary, toast,
    } = useStore();
    const [email, setEmail] = useState('');
    const [targetType, setTargetType] = useState<'join' | 'fresh'>('join');
    const valid = EMAIL_RE.test(email.trim());
    const memberCount = members.length;
    const atCapacity = memberCount + pendingInvites.filter((p) => p.targetType === 'join').length >= maxMembers;

    useEffect(() => {
        reloadMembers();
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
        const passwordMode = (getRuntimeConfig().authMode || '').toLowerCase() === 'password';
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
            .catch((err) => toast(err instanceof Error ? err.message : 'Couldn’t start cleanup'));
    };

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Sharing</h1>
                    <p className="pt-page-sub">{libraryName || 'Library'} · {memberCount} of {maxMembers} {memberCount === 1 ? 'member' : 'members'}</p>
                </div>
            </div>

            <div className="card-glass lib-card">
                {membersLoading && members.length === 0 && <div className="member-row"><span className="member-meta"><b>Loading members…</b></span></div>}
                {members.map((m) => (
                    <div key={m.userId} className="member-row">
                        <Avatar initials={initialsFor(m.email || m.userId)} />
                        <span className="member-meta"><b>{m.email || m.userId}</b><span>{m.isSelf ? 'You' : 'Member'}</span></span>
                        {m.isOwner && <span className="member-badge owner">Owner</span>}
                        {isOwner && !m.isOwner && !m.isSelf && (
                            <button type="button" className="mock-link muted" onClick={() => removeMember(m.userId)}>Remove</button>
                        )}
                    </div>
                ))}

                {pendingInvites.map((p) => (
                    <div key={p.inviteId} className="member-row pending">
                        <Avatar initials="?" />
                        <span className="member-meta"><b>{p.email}</b><span>Invited · {p.targetType === 'fresh' ? 'new library' : 'join'}</span></span>
                        {isOwner && <button type="button" className="mock-link muted" onClick={() => revokeInvite(p.inviteId)}>Revoke</button>}
                    </div>
                ))}

                {isOwner ? (
                    <div className="invite-bar">
                        <input className="field" type="email" placeholder="Invite by email" value={email} onChange={(e) => setEmail(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') send(); }} disabled={atCapacity} />
                        <select className="field field-select" value={targetType} onChange={(e) => setTargetType(e.target.value as 'join' | 'fresh')} aria-label="Invite type">
                            <option value="join">Join this library</option>
                            <option value="fresh">Start their own library</option>
                        </select>
                        <button type="button" className="btn mock-cta" disabled={!valid || atCapacity} onClick={send}>
                            <PaperAirplaneIcon className="toolbar-icon" /> Send invite
                        </button>
                    </div>
                ) : (
                    <p className="pt-page-sub" style={{ padding: '8px 4px' }}>Only the library owner can invite or remove members.</p>
                )}
                {atCapacity && isOwner && <p className="pt-page-sub" style={{ padding: '0 4px' }}>This library is at its member limit.</p>}
            </div>

            {isOwner && (
                <div className="card-glass lib-card">
                    <div className="pt-menu-label">Library settings</div>
                    <div className="pt-danger-row">
                        <button type="button" className="btn" onClick={() => void renameLibraryPrompt()}>Rename library</button>
                        <button type="button" className="btn" onClick={() => void cleanLibrary()}>Clean library</button>
                    </div>
                </div>
            )}
        </div>
    );
};

export default SharingPage;
