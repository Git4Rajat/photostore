import IndexBuildBar from './components/IndexBuildBar';
import { invalidateLocalSortIndex } from '../../services/localSortIndex';
import { invalidateLocalAlbumsIndex } from '../../services/localAlbumsIndex';
import { invalidateLocalPeopleIndex } from '../../services/localPeopleIndex';
import { subscribeLibraryChanges } from '../../services/libraryChanges';
import { perf } from '../../services/perf';
import React, { useCallback, useEffect, useState } from 'react';
import { MemoryRouter, BrowserRouter, Routes, Route } from 'react-router-dom';
import {
    Plus as PlusIcon,
    Search as MagnifyingGlassIcon,
    Image as PhotoIcon,
    Images as RectangleStackIcon,
    Users as UserGroupIcon,
    Sparkles as SparklesIcon,
    MoreHorizontal as EllipsisHorizontalIcon,
    // The active-tab "solid" cue is handled by color + a heavier stroke in the
    // tab bar (Lucide is a single outline set), so the solid aliases point at
    // the same glyphs.
    Search as MagnifyingGlassIconSolid,
    Image as PhotoIconSolid,
    Images as RectangleStackIconSolid,
    Users as UserGroupIconSolid,
    Sparkles as SparklesIconSolid,
} from 'lucide-react';
import { LogoLockup } from '../../components/shared/Logo';
import Loading from '../../components/shared/Loading';
import { AppServicesProvider, useAppServices } from '../../components/AppServicesProvider';
import { formatBytes } from '../../components/browserAiShared';
import { NotificationBell } from '../../components/AppServiceIndicators';
import { DialogHost } from '../../components/shared/dialogs';
import { getActiveAccount, initAuth, isAuthEnabled, signIn, signOut } from '../../services/authClient';
import { StoreProvider, useStore } from './store';
import { Menu } from './components/bits';
import CommandBar from './components/CommandBar';
import PhotoViewer from './components/PhotoViewer';
import Toasts from './components/Toasts';

const LazyPublicAlbumPage = React.lazy(() => import('../../components/PublicAlbumPage'));
const LazyAcceptInvitePage = React.lazy(() => import('../../components/AcceptInvitePage'));
const LazyResetPasswordPage = React.lazy(() => import('../../components/ResetPasswordPage'));
const LazyConfirmLibraryCleanPage = React.lazy(() => import('../../components/ConfirmLibraryCleanPage'));
import GalleryPage from './pages/GalleryPage';
import AlbumsPage from './pages/AlbumsPage';
import { PeoplePage, PersonDetailPage } from './pages/PeoplePages';
import ExplorePage from './pages/ExplorePage';
import AskPage from './pages/AskPage';
import SharingPage from './pages/SharingPage';
import ToolsPage from './pages/ToolsPage';
import TrashPage from './pages/TrashPage';
import AdditionalInfoPage from '../../components/AdditionalInfoPage';
import type { PageId } from './types';

const LazyLoginPage = React.lazy(() => import('../../components/LoginPage'));

// True once the user is signed in for this deployment's auth mode. Local
// no-auth deployments (isAuthEnabled false) require no sign-in, matching the
// real app's guardPrivateRoute (which renders straight through when auth is
// disabled).
const isSignedIn = (): boolean => (!isAuthEnabled() || Boolean(getActiveAccount()));

const accountInitials = (name: string, email: string): string => {
    const source = (name || email || '').trim();
    if (!source) return '?';
    const parts = source.split(/[\s@._-]+/).filter(Boolean);
    const letters = parts.length >= 2 ? parts[0][0] + parts[1][0] : source.slice(0, 2);
    return letters.toUpperCase();
};

type Theme = 'light' | 'dark' | 'system';

// A share link (…/public/album/<token>) is a public, no-auth destination. The
// prototype's own store-based navigation ignores the URL path, so without this
// the SPA would fall through to the gallery/login. Detect the path up front and
// hand it to the real PublicAlbumPage under a router that supplies :token.
const isPublicAlbumPath = (): boolean =>
    typeof window !== 'undefined' && window.location.pathname.startsWith('/public/album/');

// Same problem for an invite link (…/accept-invite?token=…): it must work for a
// brand-new invitee with no account yet, and the token query string has to
// survive, which the MemoryRouter used for the signed-out gate below discards.
const isAcceptInvitePath = (): boolean =>
    typeof window !== 'undefined' && window.location.pathname === '/accept-invite';

// Same problem for a password-reset link (…/reset-password?token=…).
const isResetPasswordPath = (): boolean =>
    typeof window !== 'undefined' && window.location.pathname === '/reset-password';

// Same problem for a library-clean confirmation link (…/confirm-library-clean?token=…).
const isConfirmLibraryCleanPath = (): boolean =>
    typeof window !== 'undefined' && window.location.pathname === '/confirm-library-clean';

const NAV: { id: PageId; label: string }[] = [
    { id: 'ask', label: 'Ask' },
    { id: 'gallery', label: 'Gallery' },
    { id: 'albums', label: 'Albums' },
    { id: 'people', label: 'People' },
    { id: 'explore', label: 'Explore' },
    { id: 'tools', label: 'Tools' },
    { id: 'sharing', label: 'Sharing' },
];
// The phone tab bar holds 5 primary destinations; the remaining ones live
// behind a "More" tab (HIG: fold overflow beyond 5 tabs into More rather than
// dropping those destinations, which previously left Tools/Sharing unreachable
// on mobile).
const MOBILE_NAV = NAV.slice(0, 5);
const MOBILE_MORE = NAV.slice(5); // Tools, Sharing

const MOBILE_NAV_ICONS: Record<string, [React.ElementType, React.ElementType]> = {
    ask: [MagnifyingGlassIcon, MagnifyingGlassIconSolid],
    gallery: [PhotoIcon, PhotoIconSolid],
    albums: [RectangleStackIcon, RectangleStackIconSolid],
    people: [UserGroupIcon, UserGroupIconSolid],
    explore: [SparklesIcon, SparklesIconSolid],
};

const applyTheme = (theme: Theme) => {
    const root = document.documentElement;
    if (theme === 'system') {
        delete root.dataset.theme;
        root.style.colorScheme = '';
    } else {
        root.dataset.theme = theme;
        root.style.colorScheme = theme;
    }
};

const activeNavFor = (page: PageId): PageId | '' => (
    page === 'person' ? 'people' : (page === 'trash' || page === 'additional') ? '' : page
);

const Page: React.FC = () => {
    const { route } = useStore();
    // Tag every request / resource / long task with the page that caused it.
    React.useEffect(() => { perf.setView(route.page); }, [route.page]);
    switch (route.page) {
        case 'ask': return <AskPage />;
        case 'gallery': return <GalleryPage />;
        case 'albums': return <AlbumsPage />;
        case 'people': return <PeoplePage />;
        case 'person': return <PersonDetailPage />;
        case 'explore': return <ExplorePage />;
        case 'sharing': return <SharingPage />;
        case 'tools': return <ToolsPage />;
        case 'trash': return <TrashPage />;
        case 'additional': return <AdditionalInfoPage />;
        default: return <GalleryPage />;
    }
};

const Topbar: React.FC<{ theme: Theme; onTheme: (t: Theme) => void; onSignOut: () => void }> = ({ theme, onTheme, onSignOut }) => {
    const { route, navigate, toast } = useStore();
    const { requestUpload } = useAppServices();
    const active = activeNavFor(route.page);
    const account = getActiveAccount();
    const accountName = account?.name || account?.username || 'Signed in';
    const accountEmail = account?.username || '';
    const initials = accountInitials(account?.name || '', account?.username || '');

    return (
        <header className="ios-header">
            <button type="button" className="pt-wordmark-btn" onClick={() => navigate('gallery')} aria-label="Go to Gallery">
                <LogoLockup size={30} className="ios-header-wordmark" />
            </button>
            <nav className="ios-tabs" aria-label="Primary navigation">
                {NAV.map((item) => (
                    <button key={item.id} type="button" className={`ios-tab${item.id === active ? ' active' : ''}`} onClick={() => navigate(item.id)}>
                        {item.label}
                    </button>
                ))}
            </nav>
            <div className="app-header-actions">
                <button type="button" className="btn mock-cta pt-upload-btn" onClick={requestUpload} aria-label="Upload">
                    <PlusIcon className="toolbar-icon" /> <span className="pt-upload-label">Upload</span>
                </button>

                <NotificationBell />

                <Menu
                    align="right"
                    renderTrigger={(toggle) => (
                        <button type="button" className="mock-avatar pt-avatar-btn" onClick={toggle} aria-label="Account">{initials}</button>
                    )}
                >
                    {(close) => (
                        <div className="pt-account-menu">
                            <div className="pt-account-head"><b>{accountName}</b>{accountEmail && <span>{accountEmail}</span>}</div>
                            <button type="button" onClick={() => { navigate('trash'); close(); }}>Recently Deleted</button>
                            <button type="button" onClick={() => { toast('Corrupted uploads: all clear'); close(); }}>Corrupted Uploads</button>
                            <button type="button" onClick={() => { navigate('additional'); close(); }}>Additional Info</button>
                            <div className="pt-account-sep" />
                            <div className="pt-account-theme">
                                <span>Theme</span>
                                <div className="mock-seg">
                                    {(['light', 'dark', 'system'] as Theme[]).map((t) => (
                                        <button key={t} type="button" className={theme === t ? 'active' : undefined} onClick={() => onTheme(t)}>{t[0].toUpperCase() + t.slice(1)}</button>
                                    ))}
                                </div>
                            </div>
                            <div className="pt-account-sep" />
                            <button type="button" onClick={() => { close(); onSignOut(); }}>Sign Out</button>
                        </div>
                    )}
                </Menu>
            </div>
        </header>
    );
};

const MobileTabbar: React.FC = () => {
    const { route, navigate } = useStore();
    const active = activeNavFor(route.page);
    const moreActive = MOBILE_MORE.some((item) => item.id === route.page);
    return (
        <nav className="mock-tabbar" aria-label="Primary navigation (mobile)">
            {MOBILE_NAV.map((item) => {
                const isActive = item.id === active;
                const [Outline, Solid] = MOBILE_NAV_ICONS[item.id];
                const Icon = isActive ? Solid : Outline;
                return (
                    <button key={item.id} type="button" className={isActive ? 'on' : undefined} onClick={() => navigate(item.id)}>
                        <Icon className="mock-tabbar-icon" aria-hidden="true" strokeWidth={isActive ? 2.5 : 2} />
                        <span>{item.label}</span>
                    </button>
                );
            })}
            <Menu
                align="right"
                className="mock-tabbar-more"
                renderTrigger={(toggle) => (
                    <button type="button" className={moreActive ? 'on' : undefined} onClick={toggle} aria-haspopup="menu" aria-label="More">
                        <EllipsisHorizontalIcon className="mock-tabbar-icon" aria-hidden="true" />
                        <span>More</span>
                    </button>
                )}
            >
                {(close) => (
                    <div className="pt-more-menu">
                        {MOBILE_MORE.map((item) => (
                            <button key={item.id} type="button" onClick={() => { navigate(item.id); close(); }}>{item.label}</button>
                        ))}
                    </div>
                )}
            </Menu>
        </nav>
    );
};

// A paused upload session (some files failed, none retried/discarded) never
// surfaced Retry/Discard in this UI -- only App.tsx (the legacy shell) ported
// them. Mirrors that banner (same appServices state, same classes from
// index.css) at the root so it's visible from every page, not just Gallery.
const UploadPausedBanner: React.FC = () => {
    const { pendingUploadSummary, pendingUploadFailedFiles, retryPersistedUploadSession, discardPersistedUploadSession, uploading } = useAppServices();
    if (!pendingUploadSummary) return null;
    return (
        <div className="upload-approval-bar root-upload-approval-bar">
            <div>
                <p className="upload-approval-title">Upload paused</p>
                <p className="upload-approval-details">
                    {pendingUploadSummary.fileCount} file(s) waiting
                    {pendingUploadSummary.failedCount > 0 ? `, ${pendingUploadSummary.failedCount} failed` : ''}
                    {pendingUploadSummary.failedCount > 0
                        ? '. If Retry can’t find them, use Upload and reselect the same photos (or the whole folder) — files already uploaded are skipped automatically, so there’s no need to pick out just the failed ones.'
                        : ''}
                </p>
                {pendingUploadFailedFiles.length > 0 && (
                    <details className="upload-approval-failed-details">
                        <summary>Show {pendingUploadFailedFiles.length} failed file(s)</summary>
                        <ul className="upload-approval-failed-list">
                            {pendingUploadFailedFiles.map((file) => (
                                <li key={file.key} className="upload-approval-failed-item">
                                    {file.previewDataUrl ? (
                                        <img src={file.previewDataUrl} alt="" className="upload-approval-failed-thumb" />
                                    ) : (
                                        <span className="upload-approval-failed-thumb upload-approval-failed-thumb-fallback">
                                            <PhotoIcon />
                                        </span>
                                    )}
                                    <span className="upload-approval-failed-info">
                                        <span className="upload-approval-failed-name">{file.name}</span>
                                        <span className="upload-approval-failed-size">{formatBytes(file.size)}</span>
                                        <span className="upload-approval-failed-reason">{file.error || 'Upload failed.'}</span>
                                    </span>
                                </li>
                            ))}
                        </ul>
                    </details>
                )}
            </div>
            <div className="upload-approval-actions">
                <button type="button" className="btn btn-primary" onClick={() => void retryPersistedUploadSession()} disabled={uploading}>
                    Retry
                </button>
                <button type="button" className="btn btn-soft" onClick={() => void discardPersistedUploadSession()} disabled={uploading}>
                    Discard
                </button>
            </div>
        </div>
    );
};

// Gates StoreProvider itself, not just what Shell renders inside it --
// StoreProvider's own mount effects (fetchPhotos, /explore, fetchAlbums,
// fetchPeople) fire unconditionally as soon as it mounts, regardless of
// which page is actually showing, so holding the gate inside Shell (i.e.
// below StoreProvider) doesn't stop them. libraryIndexReady starts null
// ("backend readiness check not back yet") and flips true as soon as that
// one check is back -- it does NOT wait for an index build to finish (that
// runs as a backend-owned background job the frontend never monitors; see
// AppServicesProvider's index-readiness effect), only for the brief existence
// check that avoids a cold account's pages all firing concurrent full scans.
const AppShellGate: React.FC<{ onSignOut: () => void }> = ({ onSignOut }) => {
    const { libraryIndexReady } = useAppServices();
    if (libraryIndexReady !== true) {
        return <Loading label="Loading Keepsake…" />;
    }
    return (
        <StoreProvider>
            <Shell onSignOut={onSignOut} />
        </StoreProvider>
    );
};

/** Re-reads what the library shows when the server finishes work that changes it (photo processing, index
 *  builds, people grouping). Without this the indexes downloaded at session start stay as they were. */
const DataRefreshBridge: React.FC = () => {
    const { registerDataRefreshHandler } = useAppServices();
    const { reloadPhotos, reloadAlbums, reloadPeople, reloadExplore, reloadTrash, applyExternalPhotoDeleteCount, applyExternalPeopleRemoval } = useStore();
    useEffect(() => registerDataRefreshHandler(() => {
        invalidateLocalSortIndex();
        invalidateLocalAlbumsIndex();
        invalidateLocalPeopleIndex();
        reloadPhotos();
        reloadAlbums();
        reloadPeople();
        reloadExplore();
    }), [registerDataRefreshHandler, reloadPhotos, reloadAlbums, reloadPeople, reloadExplore]);

    useEffect(() => subscribeLibraryChanges((change) => {
        const domains = new Set(change.domains);
        if (domains.has('photos')) {
            if (change.operation === 'photos-deleted' && change.itemCount) {
                applyExternalPhotoDeleteCount(change.itemCount);
            }
            invalidateLocalSortIndex();
            reloadPhotos();
        }
        if (domains.has('albums')) {
            invalidateLocalAlbumsIndex();
            reloadAlbums();
        }
        if (domains.has('people')) {
            if (change.entityIds?.length && (change.operation === 'person-deleted' || change.operation === 'people-deleted' || change.operation === 'people-merged')) {
                applyExternalPeopleRemoval(change.entityIds);
            }
            invalidateLocalPeopleIndex();
            reloadPeople();
        }
        if (domains.has('explore')) reloadExplore();
        if (domains.has('trash')) void reloadTrash();
    }), [reloadPhotos, reloadAlbums, reloadPeople, reloadExplore, reloadTrash, applyExternalPhotoDeleteCount, applyExternalPeopleRemoval]);
    return null;
};

const Shell: React.FC<{ onSignOut: () => void }> = ({ onSignOut }) => {
    const [theme, setTheme] = useState<Theme>('system');
    useEffect(() => applyTheme(theme), [theme]);

    return (
        <div className="pt-shell">
            <div className="mock-stage">
                <div className="mock-viewport">
                    <div className="mock-app">
                        <Topbar theme={theme} onTheme={setTheme} onSignOut={onSignOut} />
                        <div className="mock-body pt-body">
                            <UploadPausedBanner />
                            <IndexBuildBar />
                            <DataRefreshBridge />
                            <Page />
                        </div>
                        <MobileTabbar />
                    </div>
                </div>
            </div>

            <CommandBar />
            <PhotoViewer />
            <Toasts />
            <DialogHost />
        </div>
    );
};

const PrototypeApp: React.FC = () => {
    const [authReady, setAuthReady] = useState(false);
    const [signedIn, setSignedIn] = useState(false);
    const [displayName, setDisplayName] = useState('');

    const refreshAuthState = useCallback(async () => {
        setSignedIn(isSignedIn());
        setDisplayName(getActiveAccount()?.name || getActiveAccount()?.username || '');
    }, []);

    useEffect(() => {
        let mounted = true;
        void (async () => {
            if (isAuthEnabled()) {
                await initAuth();
            }
            if (!mounted) return;
            await refreshAuthState();
            setAuthReady(true);
        })();
        return () => {
            mounted = false;
        };
    }, [refreshAuthState]);

    const handleSignOut = useCallback(async () => {
        await signOut();
        await refreshAuthState();
    }, [refreshAuthState]);

    // The client-side lexical/vector search index warm-up used to start
    // unconditionally here, the moment ANY session signed in, regardless of
    // whether Ask was ever opened -- one more fetch racing five others in
    // the same mount window (see the 2026-10-01 boot-request audit). That
    // blob can be very large (hundreds of MB compressed on a big library),
    // so a session that never searches no longer pays for it at all now:
    // AskPage's own mount effect starts the same warm-up (still a retry
    // loop, since a cold account's server-side index may not have finished
    // building yet), only when the user actually opens Ask.

    // Public share links render the real album page regardless of auth state.
    if (isPublicAlbumPath()) {
        return (
            <BrowserRouter>
                <React.Suspense fallback={<Loading label="Loading album…" />}>
                    <Routes>
                        <Route path="/public/album/:token" element={<LazyPublicAlbumPage />} />
                    </Routes>
                </React.Suspense>
            </BrowserRouter>
        );
    }

    // Invite links render the real accept-invite page regardless of auth
    // state, so a brand-new invitee sees the signup (set-password) form
    // instead of falling into the generic login page.
    if (isAcceptInvitePath()) {
        return (
            <BrowserRouter>
                <React.Suspense fallback={<Loading label="Loading invitation…" />}>
                    <Routes>
                        <Route path="/accept-invite" element={<LazyAcceptInvitePage />} />
                    </Routes>
                </React.Suspense>
            </BrowserRouter>
        );
    }

    // Password-reset links render the real reset page regardless of auth state.
    if (isResetPasswordPath()) {
        return (
            <BrowserRouter>
                <React.Suspense fallback={<Loading label="Loading…" />}>
                    <Routes>
                        <Route path="/reset-password" element={<LazyResetPasswordPage />} />
                    </Routes>
                </React.Suspense>
            </BrowserRouter>
        );
    }

    // Library-clean confirmation links render the real confirm page regardless
    // of auth state (it shows its own sign-in-required message if needed).
    if (isConfirmLibraryCleanPath()) {
        return (
            <BrowserRouter>
                <React.Suspense fallback={<Loading label="Loading…" />}>
                    <Routes>
                        <Route path="/confirm-library-clean" element={<LazyConfirmLibraryCleanPage />} />
                    </Routes>
                </React.Suspense>
            </BrowserRouter>
        );
    }

    if (!authReady) {
        return <Loading label="Loading Keepsake…" />;
    }

    if (!signedIn) {
        // LoginPage relies on react-router (useNavigate/Link); a MemoryRouter
        // satisfies that without touching the URL bar. Its onAuthenticated /
        // onSignIn callbacks re-check auth state, which flips this gate.
        return (
            <MemoryRouter>
                <React.Suspense fallback={<Loading label="Loading sign-in…" />}>
                    <LazyLoginPage
                        authEnabled={isAuthEnabled()}
                        authReady={authReady}
                        displayName={displayName}
                        onSignIn={async () => { await signIn(); await refreshAuthState(); }}
                        onAuthenticated={refreshAuthState}
                    />
                </React.Suspense>
            </MemoryRouter>
        );
    }

    return (
        <AppServicesProvider>
            <AppShellGate onSignOut={handleSignOut} />
        </AppServicesProvider>
    );
};

export default PrototypeApp;
