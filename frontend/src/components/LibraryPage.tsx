import React, { useCallback, useEffect, useRef, useState } from 'react';
import * as library from '../services/libraryClient';
import { startLibraryExport, ExportController, ExportProgress } from '../services/libraryExportDownloader';
import { verifyOpfsUsable } from '../services/opfsDownloadStaging';
import { getRuntimeConfig } from '../config/appConfig';
import { Loading } from './shared/Loading';
import { confirmDialog } from './shared/dialogs';

const isPasswordAuthMode = (): boolean => (getRuntimeConfig().authMode || '').toLowerCase() === 'password';

// Marks "download entire library" as in-flight across page reloads, scoped
// per library so switching libraries doesn't show one library's export state
// on another's. Only a flag, not the progress itself: startLibraryExport
// figures out exactly what's left to download from its own IndexedDB records
// (see libraryExportDownloader.ts), so a reload just needs to know whether to
// call it again automatically instead of waiting for another click.
const exportActiveStorageKey = (libraryId: string): string => `photostore.libraryExportActive.${libraryId}`;

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

    const [exportOutcome, setExportOutcome] = useState<'idle' | 'running' | 'done'>('idle');
    const [exportProgress, setExportProgress] = useState<ExportProgress | null>(null);
    const exportControllerRef = useRef<ExportController | null>(null);
    // null while the async probe is still running -- confirmed live 2026-09-06
    // that a browser can expose navigator.storage.getDirectory as a function
    // while it actually rejects every call ("operation failed for an unknown
    // transient reason"), observed consistently in WebKit. A shape check
    // alone can't catch that, so this actually calls it once; without it,
    // every file in the export failed individually with that cryptic message
    // while the UI still ended on "Done". Kept as an initial-mount check
    // rather than one call per export start -- if OPFS is unusable, its own
    // handle stays unusable for the rest of the page's life.
    const [opfsUsable, setOpfsUsable] = useState<boolean | null>(null);
    // Idempotency guard for startExport itself (see there) -- the button
    // below is only disabled via React state (`busy`, which this flow never
    // sets), so two clicks landing before a re-render, or a manual click
    // racing the auto-resume effect, would otherwise launch two concurrent
    // startLibraryExport orchestrators against the same IndexedDB/OPFS
    // records with no de-dup between them. Same class of bug the existing
    // sendInFlightRef above guards against for the invite form.
    const exportRunningRef = useRef(false);

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

    // Starts (or transparently resumes -- startLibraryExport figures out
    // what's already done via its own IndexedDB records, see
    // libraryExportDownloader.ts) the client-orchestrated export, and flags
    // it active in localStorage so a reload can restart it automatically
    // instead of silently going idle mid-download.
    const startExport = useCallback((libraryId: string) => {
        if (exportRunningRef.current) return;
        exportRunningRef.current = true;
        try {
            window.localStorage.setItem(exportActiveStorageKey(libraryId), '1');
        } catch {
            // Best-effort only -- losing this just means a reload mid-export
            // won't auto-resume without another click.
        }
        setExportOutcome('running');
        setExportProgress(null);
        exportControllerRef.current = startLibraryExport(
            libraryId,
            (progress) => setExportProgress(progress),
            () => {
                try {
                    window.localStorage.removeItem(exportActiveStorageKey(libraryId));
                } catch {
                    // Not fatal -- worst case a finished export looks
                    // resumable on the next reload, and immediately no-ops
                    // since every file is already marked 'saved'.
                }
                exportControllerRef.current = null;
                exportRunningRef.current = false;
                setExportOutcome('done');
            },
        );
    }, []);

    useEffect(() => {
        let cancelled = false;
        verifyOpfsUsable().then((ok) => {
            if (!cancelled) setOpfsUsable(ok);
        });
        return () => { cancelled = true; };
    }, []);

    // Auto-resume an export that was still running when the page last
    // unloaded. Waits for opfsUsable === true (not just "not false") so a
    // reload can't race ahead of the probe and start fetching/staging before
    // we know OPFS actually works here. No separate de-dup guard needed here
    // beyond startExport's own exportRunningRef -- that's the single choke
    // point every caller (this effect, the button) goes through.
    useEffect(() => {
        const libraryId = mine?.activeLibraryId;
        if (!libraryId || opfsUsable !== true) return;
        let active = false;
        try {
            active = window.localStorage.getItem(exportActiveStorageKey(libraryId)) === '1';
        } catch {
            active = false;
        }
        if (active) startExport(libraryId);
    }, [mine?.activeLibraryId, opfsUsable, startExport]);

    const handleStartExport = () => {
        const libraryId = mine?.activeLibraryId;
        if (libraryId && opfsUsable) startExport(libraryId);
    };

    const handleCancelExport = () => {
        exportControllerRef.current?.cancel();
        exportControllerRef.current = null;
        exportRunningRef.current = false;
        const libraryId = mine?.activeLibraryId;
        if (libraryId) {
            try {
                window.localStorage.removeItem(exportActiveStorageKey(libraryId));
            } catch {
                // Best-effort only.
            }
        }
        setExportOutcome('idle');
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
                            Downloads every photo and video in this library straight to your browser's default
                            download location — there's no folder picker, so if you want them somewhere
                            specific, point your browser's download setting there first. Turning off "ask
                            where to save each file" beforehand is also worth doing, or a large library turns
                            into a save dialog per file. If a file shows as failed partway through, check for
                            a "this site is downloading multiple files" notice near your address bar and
                            allow it — most browsers pause automatic downloads until you approve that once.
                        </p>
                        {opfsUsable === false ? (
                            <p className="status error">
                                Your browser is blocking the private storage this download needs in order to
                                resume safely — this happens in Private/Incognito browsing in some browsers.
                                Try a normal (non-private) window, or the latest Chrome, Edge, or Firefox.
                            </p>
                        ) : opfsUsable === null ? (
                            <p className="status">Checking browser support…</p>
                        ) : exportOutcome === 'running' ? (
                            <>
                                <p className="status">
                                    {exportProgress?.listingComplete
                                        ? `Downloading… ${exportProgress.filesSaved} of ${exportProgress.filesSeen} files saved.`
                                        : `Downloading… ${exportProgress?.filesSaved ?? 0} files saved so far (still counting the library).`}
                                    {exportProgress?.currentFilename ? ` Current: ${exportProgress.currentFilename}` : ''}
                                </p>
                                {exportProgress?.fatalError && (
                                    <p className="status error">{exportProgress.fatalError} It will pick back up from here next time.</p>
                                )}
                                {(exportProgress?.errors.length ?? 0) > 0 && (
                                    <p className="status error">
                                        {exportProgress!.errors.length} file(s) could not be downloaded and were skipped.
                                    </p>
                                )}
                                <button type="button" className="btn btn-soft" onClick={handleCancelExport}>Stop</button>
                            </>
                        ) : exportOutcome === 'done' ? (
                            <>
                                {exportProgress?.fatalError ? (
                                    <p className="status error">
                                        Stopped before finishing — {exportProgress.fatalError} {exportProgress.filesSaved} file(s) were saved before that happened.
                                    </p>
                                ) : (
                                    <p className="status success">
                                        Done — {exportProgress?.filesSaved ?? 0} file(s) saved to your downloads.
                                        {(exportProgress?.errors.length ?? 0) > 0
                                            ? ` ${exportProgress!.errors.length} file(s) could not be downloaded.`
                                            : ''}
                                    </p>
                                )}
                                <button type="button" className="btn btn-soft" disabled={busy} onClick={handleStartExport}>
                                    {exportProgress?.fatalError ? 'Try again' : 'Download again'}
                                </button>
                            </>
                        ) : (
                            <button type="button" className="btn btn-soft" disabled={busy} onClick={handleStartExport}>
                                Download entire library
                            </button>
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
