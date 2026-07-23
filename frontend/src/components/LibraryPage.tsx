import React, { useCallback, useEffect, useState } from 'react';
import * as library from '../services/libraryClient';

// Manage shared libraries: switch between the ones you belong to, and (for the
// owner) invite people, see members, and rename/delete the library.
const LibraryPage: React.FC = () => {
    const [mine, setMine] = useState<library.MineResponse | null>(null);
    const [members, setMembers] = useState<library.MembersResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');

    const [inviteEmail, setInviteEmail] = useState('');
    const [inviteType, setInviteType] = useState<'join' | 'fresh'>('join');
    const [renameValue, setRenameValue] = useState('');

    const load = useCallback(async () => {
        setError('');
        try {
            const [mineResp, membersResp] = await Promise.all([library.getMine(), library.getMembers()]);
            setMine(mineResp);
            setMembers(membersResp);
            setRenameValue(membersResp.name || '');
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Could not load your libraries.');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        load();
    }, [load]);

    const run = async (fn: () => Promise<void>, successMessage = '') => {
        setBusy(true);
        setError('');
        setNotice('');
        try {
            await fn();
            if (successMessage) {
                setNotice(successMessage);
            }
            await load();
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Something went wrong.');
        } finally {
            setBusy(false);
        }
    };

    // Changing the active library re-issues the session token; a full reload is
    // the simplest way to refresh every view (gallery, people, albums) that had
    // already fetched data for the previous library.
    const runThenReload = async (fn: () => Promise<void>) => {
        setBusy(true);
        setError('');
        try {
            await fn();
            window.location.reload();
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Something went wrong.');
            setBusy(false);
        }
    };

    if (loading) {
        return <section className="card-glass"><p className="status">Loading libraries…</p></section>;
    }

    const activeId = mine?.activeLibraryId || '';
    const isOwner = Boolean(members?.isOwner);
    const memberCount = members?.members.length ?? 0;
    const pending = members?.pendingInvites ?? [];
    const maxMembers = members?.maxMembers ?? mine?.maxMembers ?? 15;
    const atCapacity = memberCount + pending.filter((p) => p.targetType === 'join').length >= maxMembers;
    const isPrimaryOwnerLib = members?.ownerUserId === 'owner' && activeId === 'owner';

    return (
        <section className="library-page">
            {error && <p className="status error">{error}</p>}
            {notice && <p className="status success">{notice}</p>}

            <div className="card-glass">
                <h2 className="auth-page-title">Your libraries</h2>
                <ul className="library-list">
                    {(mine?.libraries ?? []).map((lib) => {
                        const active = lib.libraryId === activeId;
                        return (
                            <li key={lib.libraryId} className={`library-item${active ? ' active' : ''}`}>
                                <div>
                                    <strong>{lib.name || (lib.isOwner ? 'My library' : 'Shared library')}</strong>
                                    {lib.isOwner && <span className="badge"> owner</span>}
                                    {active && <span className="badge"> active</span>}
                                </div>
                                {!active && (
                                    <button
                                        type="button"
                                        className="btn btn-soft"
                                        disabled={busy}
                                        onClick={() => runThenReload(async () => { await library.switchLibrary(lib.libraryId); })}
                                    >
                                        Switch
                                    </button>
                                )}
                            </li>
                        );
                    })}
                </ul>
            </div>

            <div className="card-glass">
                <h2 className="auth-page-title">Members of “{members?.name || 'this library'}”</h2>
                <p className="status">{memberCount} of {maxMembers} members</p>
                <ul className="library-list">
                    {(members?.members ?? []).map((m) => (
                        <li key={m.userId} className="library-item">
                            <div>
                                <strong>{m.email || m.userId}</strong>
                                {m.isOwner && <span className="badge"> owner</span>}
                                {m.isSelf && <span className="badge"> you</span>}
                            </div>
                            {isOwner && !m.isSelf && (
                                <button
                                    type="button"
                                    className="btn btn-soft"
                                    disabled={busy}
                                    onClick={() => run(async () => { await library.removeMember(m.userId); }, 'Member removed.')}
                                >
                                    Remove
                                </button>
                            )}
                        </li>
                    ))}
                </ul>

                {isOwner && pending.length > 0 && (
                    <>
                        <h3 className="status">Pending invitations</h3>
                        <ul className="library-list">
                            {pending.map((p) => (
                                <li key={p.inviteId} className="library-item">
                                    <div>{p.email} <span className="badge">{p.targetType}</span></div>
                                    <button
                                        type="button"
                                        className="btn btn-soft"
                                        disabled={busy}
                                        onClick={() => run(async () => { await library.revokePendingInvite(p.inviteId); }, 'Invitation revoked.')}
                                    >
                                        Revoke
                                    </button>
                                </li>
                            ))}
                        </ul>
                    </>
                )}

                {/* A member (not the owner) can leave a shared library. */}
                {!isOwner && (
                    <button
                        type="button"
                        className="btn btn-soft"
                        disabled={busy}
                        onClick={() => runThenReload(async () => { await library.leaveLibrary(activeId); })}
                    >
                        Leave this library
                    </button>
                )}
            </div>

            {isOwner && (
                <div className="card-glass">
                    <h2 className="auth-page-title">Invite someone</h2>
                    <form
                        className="local-login"
                        onSubmit={(e) => {
                            e.preventDefault();
                            const email = inviteEmail.trim();
                            if (!email) return;
                            run(async () => {
                                await library.sendInvite(email, inviteType);
                                setInviteEmail('');
                            }, 'Invitation sent.');
                        }}
                    >
                        <label htmlFor="invite-email">Their email</label>
                        <input
                            id="invite-email"
                            className="field"
                            type="email"
                            value={inviteEmail}
                            onChange={(e) => setInviteEmail(e.target.value)}
                            placeholder="person@example.com"
                        />
                        <label htmlFor="invite-type">Invitation type</label>
                        <select
                            id="invite-type"
                            className="field"
                            value={inviteType}
                            onChange={(e) => setInviteType(e.target.value === 'fresh' ? 'fresh' : 'join')}
                        >
                            <option value="join">Join this library (they see these photos)</option>
                            <option value="fresh">Use the app fresh (their own new library)</option>
                        </select>
                        {inviteType === 'join' && atCapacity && (
                            <p className="status error">This library is full ({maxMembers} max). Remove a member first.</p>
                        )}
                        <button
                            type="submit"
                            className="btn btn-primary"
                            disabled={busy || !inviteEmail.trim() || (inviteType === 'join' && atCapacity)}
                        >
                            Send invitation
                        </button>
                    </form>
                </div>
            )}

            {isOwner && (
                <div className="card-glass">
                    <h2 className="auth-page-title">Library settings</h2>
                    <form
                        className="local-login"
                        onSubmit={(e) => {
                            e.preventDefault();
                            run(async () => { await library.renameLibrary(renameValue.trim()); }, 'Library renamed.');
                        }}
                    >
                        <label htmlFor="library-name">Library name</label>
                        <input
                            id="library-name"
                            className="field"
                            type="text"
                            value={renameValue}
                            maxLength={100}
                            onChange={(e) => setRenameValue(e.target.value)}
                        />
                        <button type="submit" className="btn btn-soft" disabled={busy}>Save name</button>
                    </form>

                    {!isPrimaryOwnerLib && (
                        <button
                            type="button"
                            className="btn btn-soft danger"
                            disabled={busy || memberCount > 1}
                            title={memberCount > 1 ? 'Remove all other members first' : undefined}
                            onClick={() => {
                                if (window.confirm('Delete this library and all its photos? This cannot be undone.')) {
                                    run(async () => { await library.deleteLibrary(); }, 'Library deleted.');
                                }
                            }}
                        >
                            Delete library
                        </button>
                    )}
                </div>
            )}
        </section>
    );
};

export default LibraryPage;
