import React, { useCallback, useEffect, useRef, useState } from 'react';
import * as library from '../services/libraryClient';
import { getRuntimeConfig } from '../config/appConfig';
import { Loading } from './shared/Loading';
import { confirmDialog } from './shared/dialogs';

const isPasswordAuthMode = (): boolean => (getRuntimeConfig().authMode || '').toLowerCase() === 'password';

// Persists "download entire library" progress across page reloads, scoped per
// library so switching libraries doesn't show one library's export state on
// another's. Without this, a refresh mid-export (or even just navigating away
// and back) lost all progress and forced the user to babysit the tab -- the
// export itself is a fire-and-forget background job server-side, so the
// frontend has no reason to lose track of it too.
type StoredDownloadState = {
    jobId: string;
    outcome: 'pending' | 'done' | 'failed';
    result: library.DownloadStatusResult['result'] | null;
    error: string;
};

const downloadStorageKey = (libraryId: string): string => `photostore.libraryDownload.${libraryId}`;

const loadStoredDownloadState = (libraryId: string): StoredDownloadState | null => {
    try {
        const raw = window.localStorage.getItem(downloadStorageKey(libraryId));
        return raw ? (JSON.parse(raw) as StoredDownloadState) : null;
    } catch {
        return null;
    }
};

// Manage shared libraries: switch between the ones you belong to, and (for the
// owner) invite people, see members, and rename/delete the library.
const LibraryPage: React.FC = () => {
    const [mine, setMine] = useState<library.MineResponse | null>(null);
    const [members, setMembers] = useState<library.MembersResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [cleanupNotice, setCleanupNotice] = useState('');
    const cleanupPollTimer = useRef<number | null>(null);
    const lastSeenCleanupTime = useRef<number>(0);
    // Belt-and-suspenders against double submission: `disabled={busy}` covers
    // mouse clicks once React re-renders, but a ref flips synchronously so a
    // second Enter/submit landing before that re-render can't slip through.
    const sendInFlightRef = useRef(false);

    const [inviteEmail, setInviteEmail] = useState('');
    const [inviteType, setInviteType] = useState<'join' | 'fresh'>('join');
    const [sendingInvite, setSendingInvite] = useState(false);
    const [inviteError, setInviteError] = useState('');
    const [renameValue, setRenameValue] = useState('');

    const [showCleanForm, setShowCleanForm] = useState(false);
    const [cleanPassword, setCleanPassword] = useState('');
    const [cleanNotice, setCleanNotice] = useState<library.CleanRequestResult | null>(null);

    const [downloadJobId, setDownloadJobId] = useState('');
    const [downloadOutcome, setDownloadOutcome] = useState<'idle' | 'pending' | 'done' | 'failed'>('idle');
    const [downloadResult, setDownloadResult] = useState<library.DownloadStatusResult['result'] | null>(null);
    const [downloadError, setDownloadError] = useState('');
    const [downloadRequesting, setDownloadRequesting] = useState(false);
    const downloadPollTimer = useRef<number | null>(null);

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

    // Poll for cleanup completion
    useEffect(() => {
        const pollCleanupStatus = async () => {
            try {
                const info = await library.getLibraryCleanupInfo();
                if (info.lastCleanupStatus === 'completed' && info.lastCleanupTime > lastSeenCleanupTime.current) {
                    lastSeenCleanupTime.current = info.lastCleanupTime;
                    const photosStr = info.lastCleanupPhotosDeleted > 0 
                        ? `${info.lastCleanupPhotosDeleted} photo(s)/video(s) removed` 
                        : 'all photos and videos removed';
                    setCleanupNotice(`Cleanup complete — ${photosStr}. You can now upload new photos.`);
                    // Reload the gallery after cleanup
                    await load();
                    // Auto-dismiss the notification after 10 seconds
                    setTimeout(() => setCleanupNotice(''), 10000);
                }
            } catch {
                // Silently ignore cleanup status check failures
            }
        };

        cleanupPollTimer.current = window.setInterval(pollCleanupStatus, 3000);
        return () => {
            if (cleanupPollTimer.current) window.clearInterval(cleanupPollTimer.current);
        };
    }, [load]);

    // Restore any in-flight or completed export for the active library once
    // it's known, so a reload lands back on live progress (resumes polling
    // via the effect below, unchanged) or a ready-to-download link instead of
    // the idle "Download entire library" button.
    useEffect(() => {
        const libraryId = mine?.activeLibraryId;
        if (!libraryId) return;
        const stored = loadStoredDownloadState(libraryId);
        if (!stored) return;
        setDownloadJobId(stored.jobId);
        setDownloadOutcome(stored.outcome);
        setDownloadResult(stored.result);
        setDownloadError(stored.error);
    }, [mine?.activeLibraryId]);

    // Keep that stored state in sync so it survives the next reload too.
    useEffect(() => {
        const libraryId = mine?.activeLibraryId;
        if (!libraryId || downloadOutcome === 'idle') return;
        const stored: StoredDownloadState = {
            jobId: downloadJobId,
            outcome: downloadOutcome,
            result: downloadResult,
            error: downloadError,
        };
        try {
            window.localStorage.setItem(downloadStorageKey(libraryId), JSON.stringify(stored));
        } catch {
            // Best-effort only (e.g. storage quota/private browsing) -- losing
            // persistence isn't worse than the pre-existing in-memory-only behavior.
        }
    }, [mine?.activeLibraryId, downloadJobId, downloadOutcome, downloadResult, downloadError]);

    // Poll for the "download entire library" export job while one is in flight.
    useEffect(() => {
        if (!downloadJobId || downloadOutcome === 'done' || downloadOutcome === 'failed') return undefined;
        const poll = async () => {
            try {
                const result = await library.getLibraryDownloadStatus(downloadJobId);
                if (result.status === 'done' || result.status === 'failed') {
                    setDownloadResult(result.result || null);
                    setDownloadError(result.error || '');
                    setDownloadOutcome(result.status as 'done' | 'failed');
                    return;
                }
                // Still running: the export heartbeats photosCompleted/photosTotal
                // periodically, so surface that as live progress while we keep polling.
                if (result.result && (result.result.photosCompleted !== undefined || result.result.photosTotal !== undefined)) {
                    setDownloadResult(result.result);
                }
            } catch {
                // Keep polling; a transient status-check failure isn't fatal.
            }
            downloadPollTimer.current = window.setTimeout(poll, 3000);
        };
        poll();
        return () => {
            if (downloadPollTimer.current) window.clearTimeout(downloadPollTimer.current);
        };
    }, [downloadJobId, downloadOutcome]);

    const handleDownloadLibrary = async () => {
        setDownloadError('');
        setDownloadResult(null);
        setDownloadRequesting(true);
        try {
            const result = await library.requestLibraryDownload();
            if (result.jobId) {
                setDownloadJobId(result.jobId);
                setDownloadOutcome('pending');
            } else {
                setDownloadOutcome('failed');
                setDownloadError('Could not start the library export.');
            }
        } catch (e) {
            setDownloadOutcome('failed');
            setDownloadError(e instanceof Error ? e.message : 'Could not start the library export.');
        } finally {
            setDownloadRequesting(false);
        }
    };

    const formatExportSize = (bytes?: number): string => {
        if (!bytes) return '';
        const mb = bytes / (1024 * 1024);
        return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(1)} MB`;
    };

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
        return <section className="card-glass"><Loading label="Loading libraries…" /></section>;
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
            {cleanupNotice && <p className="status success">{cleanupNotice}</p>}

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
                            if (sendInFlightRef.current) return;
                            const email = inviteEmail.trim();
                            if (!email) return;
                            sendInFlightRef.current = true;
                            setSendingInvite(true);
                            setInviteError('');
                            setBusy(true);
                            setError('');
                            setNotice('');
                            (async () => {
                                try {
                                    await library.sendInvite(email, inviteType);
                                    setInviteEmail('');
                                    setNotice('Invitation sent.');
                                    await load();
                                } catch (e2) {
                                    // Shown right next to the form — the page-top banner can
                                    // scroll out of view, which made "that email already has a
                                    // pending invite" look like the click did nothing.
                                    setInviteError(e2 instanceof Error ? e2.message : 'Could not send the invitation.');
                                } finally {
                                    sendInFlightRef.current = false;
                                    setBusy(false);
                                    setSendingInvite(false);
                                }
                            })();
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
                        {inviteError && <p className="status error">{inviteError}</p>}
                        <button
                            type="submit"
                            className="btn btn-primary"
                            disabled={busy || !inviteEmail.trim() || (inviteType === 'join' && atCapacity)}
                        >
                            {sendingInvite ? 'Sending…' : 'Send invitation'}
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

                    <div className="library-clean-section">
                        <h3 className="status">Download library</h3>
                        <p className="status">
                            Get a ZIP of every photo and video in this library. Large libraries can take a
                            while to prepare.
                        </p>
                        {downloadOutcome === 'done' && downloadResult && (downloadResult.parts?.length ?? 0) > 1 ? (
                            <div className="status success">
                                <p>
                                    Export ready — {downloadResult.photosIncluded ?? 0} photo(s) across {downloadResult.parts!.length} parts
                                    {downloadResult.sizeBytes ? `, ${formatExportSize(downloadResult.sizeBytes)}` : ''}
                                    {downloadResult.photosSkipped ? ` (${downloadResult.photosSkipped} could not be included)` : ''}.
                                </p>
                                <ul className="library-list">
                                    {downloadResult.parts!.map((part) => (
                                        <li key={part.partIndex} className="library-item">
                                            <div>
                                                Part {part.partIndex} — {part.photosIncluded} photo(s)
                                                {part.sizeBytes ? `, ${formatExportSize(part.sizeBytes)}` : ''}
                                            </div>
                                            <a className="btn btn-soft" href={part.downloadUrl} download>Download</a>
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        ) : downloadOutcome === 'done' && downloadResult ? (
                            <p className="status success">
                                Export ready — {downloadResult.photosIncluded ?? 0} photo(s)
                                {downloadResult.sizeBytes ? `, ${formatExportSize(downloadResult.sizeBytes)}` : ''}
                                {downloadResult.photosSkipped ? ` (${downloadResult.photosSkipped} could not be included)` : ''}.{' '}
                                {downloadResult.parts?.[0]?.downloadUrl && (
                                    <a href={downloadResult.parts[0].downloadUrl} download>Download ZIP</a>
                                )}
                            </p>
                        ) : downloadOutcome === 'pending' ? (
                            <p className="status">
                                {downloadResult?.photosTotal
                                    ? `Preparing your download… ${downloadResult.photosCompleted ?? 0} of ${downloadResult.photosTotal} photos processed.`
                                    : 'Preparing your download…'}
                            </p>
                        ) : (
                            <>
                                {downloadError && <p className="status error">{downloadError}</p>}
                                <button
                                    type="button"
                                    className="btn btn-soft"
                                    disabled={busy || downloadRequesting}
                                    onClick={handleDownloadLibrary}
                                >
                                    {downloadRequesting ? 'Starting…' : 'Download entire library'}
                                </button>
                            </>
                        )}
                    </div>

                    <div className="library-clean-section">
                        <h3 className="status">Clean library</h3>
                        <p className="status">
                            Permanently delete every photo and video in this library. The library itself, its
                            members, and their accounts are kept — only the content is wiped. This cannot be undone.
                        </p>
                        {cleanNotice ? (
                            <p className="status success">
                                Confirmation email sent to {cleanNotice.sentTo.join(' and ')}.
                                {cleanNotice.requiresAdditionalApproval
                                    ? ' Since this library is shared, both confirmations are required before cleanup runs.'
                                    : ' Click the link in that email to proceed.'}
                            </p>
                        ) : showCleanForm ? (
                            <form
                                className="local-login"
                                onSubmit={(e) => {
                                    e.preventDefault();
                                    run(async () => {
                                        const result = await library.requestLibraryClean(
                                            isPasswordAuthMode() ? cleanPassword : undefined,
                                        );
                                        setCleanNotice(result);
                                        setCleanPassword('');
                                        setShowCleanForm(false);
                                    });
                                }}
                            >
                                {isPasswordAuthMode() && (
                                    <>
                                        <label htmlFor="clean-password">Confirm your password</label>
                                        <input
                                            id="clean-password"
                                            className="field"
                                            type="password"
                                            autoComplete="current-password"
                                            value={cleanPassword}
                                            onChange={(e) => setCleanPassword(e.target.value)}
                                        />
                                    </>
                                )}
                                <div className="library-clean-actions">
                                    <button
                                        type="submit"
                                        className="btn btn-soft danger"
                                        disabled={busy || (isPasswordAuthMode() && !cleanPassword)}
                                    >
                                        Send confirmation email
                                    </button>
                                    <button
                                        type="button"
                                        className="btn btn-link"
                                        disabled={busy}
                                        onClick={() => { setShowCleanForm(false); setCleanPassword(''); }}
                                    >
                                        Cancel
                                    </button>
                                </div>
                            </form>
                        ) : (
                            <button
                                type="button"
                                className="btn btn-soft danger"
                                disabled={busy}
                                onClick={() => setShowCleanForm(true)}
                            >
                                Clean library
                            </button>
                        )}
                    </div>

                    {!isPrimaryOwnerLib && (
                        <button
                            type="button"
                            className="btn btn-soft danger"
                            disabled={busy || memberCount > 1}
                            title={memberCount > 1 ? 'Remove all other members first' : undefined}
                            onClick={async () => {
                                const confirmed = await confirmDialog({
                                    title: 'Delete library',
                                    message: 'Delete this library and all its photos? This cannot be undone.',
                                    confirmLabel: 'Delete forever',
                                    danger: true,
                                });
                                if (confirmed) {
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
