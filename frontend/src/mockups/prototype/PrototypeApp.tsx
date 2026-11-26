import IndexBuildBar from './components/IndexBuildBar';
import UploadDock from './components/UploadDock';
import ProcessingDock from './components/ProcessingDock';
import { WidgetStackProvider } from './components/WidgetStack';
import { clearLocalSortIndexRemovals, invalidateLocalSortIndex, removeLocalSortIndexRows } from '../../services/localSortIndex';
import { invalidateLocalAlbumsIndex } from '../../services/localAlbumsIndex';
import { invalidateLocalPeopleIndex } from '../../services/localPeopleIndex';
import { subscribeLibraryChanges } from '../../services/libraryChanges';
import { perf } from '../../services/perf';
import React, { useCallback, useEffect, useState } from 'react';
import { MemoryRouter, BrowserRouter, Routes, Route } from 'react-router-dom';
// The active-tab "solid" cue is colour plus a heavier stroke in the tab bar (the redesign is a single
// outline set), so the solid aliases point at the same glyphs.
import { SearchGlyph as MagnifyingGlassIcon, PhotosGlyph as PhotoIcon, AlbumsGlyph as RectangleStackIcon, UploadGlyph as UploadIcon, PhotosGlyph as PhotoIconSolid, AlbumsGlyph as RectangleStackIconSolid, SearchGlyph as MagnifyingGlassIconSolid } from './components/RedesignIcons';
import { NAV_ITEMS } from './components/HomeChrome';
import Loading from '../../components/shared/Loading';
import { CachedGalleryPreview, PreviewUntilGallery, useHomeSnapshot } from './components/CachedGalleryPreview';
import { HomeSnapshotSaver } from './components/HomeSnapshotSaver';
import { AppServicesProvider, useAppServices } from '../../components/AppServicesProvider';
import { NotificationBell } from '../../components/AppServiceIndicators';
import { DialogHost } from '../../components/shared/dialogs';
import { getActiveAccount, initAuth, isAuthEnabled, signIn, signOut } from '../../services/authClient';
import { StoreProvider, useStore } from './store';

import type { Route as AppRoute } from './types';
import AppSidebar from './components/AppSidebar';
import { Menu } from './components/bits';
import { LibrarySwitcher } from './components/LibrarySwitcher';
import CommandBar from './components/CommandBar';
import PhotoViewer from './components/PhotoViewer';
import Toasts from './components/Toasts';
import { ScrollToTopButton } from './components/ScrollToTopButton';
import { StoreFloatingSelectButton } from './components/StoreFloatingSelectButton';

const LazyPublicAlbumPage = React.lazy(() => import('../../components/PublicAlbumPage'));
const LazyAcceptInvitePage = React.lazy(() => import('../../components/AcceptInvitePage'));
const LazyResetPasswordPage = React.lazy(() => import('../../components/ResetPasswordPage'));
const LazyLibraryCleanApprovalDialog = React.lazy(() => import('../../components/LibraryCleanApprovalDialog'));
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
import { applyTheme, loadTheme, saveTheme, type Theme } from './theme';
import { initialsFor } from '../../utils/format';

const LazyLoginPage = React.lazy(() => import('../../components/LoginPage'));

// True once the user is signed in for this deployment's auth mode. Local
// no-auth deployments (isAuthEnabled false) require no sign-in, matching the
// real app's guardPrivateRoute (which renders straight through when auth is
// disabled).
const isSignedIn = (): boolean => (!isAuthEnabled() || Boolean(getActiveAccount()));

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

const NAV: { id: PageId; label: string }[] = [...NAV_ITEMS];
// The phone tab bar holds the three places people live in. People and Explore
// are reached from the Albums tab; Library & sharing, Recently Deleted and
// Tools sit in the account menu.
const MOBILE_NAV = NAV.slice(0, 3);

const MOBILE_NAV_ICONS: Record<string, [React.ElementType, React.ElementType]> = {
    ask: [MagnifyingGlassIcon, MagnifyingGlassIconSolid],
    gallery: [PhotoIcon, PhotoIconSolid],
    albums: [RectangleStackIcon, RectangleStackIconSolid],
};

const ACCOUNT_PAGES: PageId[] = ['trash', 'additional', 'tools', 'sharing'];
const activeNavFor = (page: PageId): PageId | '' => (
    page === 'person' ? 'people' : ACCOUNT_PAGES.includes(page) ? '' : page
);
// On a phone People and Explore live under the Albums tab.
const activeMobileNavFor = (page: PageId): PageId | '' => {
    const active = activeNavFor(page);
    return active === 'people' || active === 'explore' ? 'albums' : active;
};

const PAGE_TITLES: Record<PageId, string> = {
    gallery: 'Photos', ask: 'Search', albums: 'Albums', people: 'People', person: 'People',
    explore: 'Places and things', sharing: 'Library', tools: 'Tools', trash: 'Recently deleted', additional: 'About',
};
/** What the phone header calls the page. An open album still says Albums: its cover banner carries the name. */
const pageTitle = (route: AppRoute): string => PAGE_TITLES[route.page] ?? 'Keepsake';

const Page: React.FC = () => {
    const { route } = useStore();
    // Tag every request / resource / long task with the page that caused it.
    React.useEffect(() => { perf.setView(route.page); }, [route.page]);
    switch (route.page) {
        case 'ask': return <AskPage />;
        case 'gallery': return <GalleryPage />;
        case 'albums': return <AlbumsPage />;
        case 'people': return <PeoplePage />;
        // Desktops keep the People grid beside the person; phones show the person alone.
        case 'person': return (
            <div className="pt-people-split">
                <div className="pt-people-split-list"><PeoplePage /></div>
                <aside className="pt-people-split-panel" aria-label="Person"><PersonDetailPage /></aside>
            </div>
        );
        case 'explore': return <ExplorePage />;
        case 'sharing': return <SharingPage />;
        case 'tools': return <ToolsPage />;
        case 'trash': return <TrashPage />;
        case 'additional': return <AdditionalInfoPage />;
        default: return <GalleryPage />;
    }
};

const Topbar: React.FC<{ theme: Theme; onTheme: (t: Theme) => void; onSignOut: () => void }> = ({ theme, onTheme, onSignOut }) => {
    const { route, navigate } = useStore();
    const { requestUpload } = useAppServices();
    const active = activeNavFor(route.page);
    const account = getActiveAccount();
    const accountName = account?.name || account?.username || 'Signed in';
    const accountEmail = account?.username || '';
    const initials = initialsFor(account?.name || account?.username || '');
    // "/" opens Search from anywhere, unless you're typing.
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return;
            const el = e.target as HTMLElement | null;
            if (el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) return;
            e.preventDefault();
            navigate('ask');
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [navigate]);

    return (
        <header className="ios-header">
            {/* Phones: the page's own title sits in the header row (the page hides its
                copy). Desktops: the sidebar names the page, and the header holds search. */}
            <h1 className="pt-header-title">{pageTitle(route)}</h1>
            <button type="button" className="pt-header-search" onClick={() => navigate('ask')}>
                <MagnifyingGlassIcon aria-hidden="true" /> <span>Search people, places and things</span><kbd aria-hidden="true">/</kbd>
            </button>
            <nav className="ios-tabs" aria-label="Primary navigation">
                {NAV.map((item) => (
                    <button key={item.id} type="button" className={`ios-tab${item.id === active ? ' active' : ''}`} aria-current={item.id === active ? 'page' : undefined} onClick={() => navigate(item.id)}>
                        {item.label}
                    </button>
                ))}
            </nav>
            <div className="app-header-actions">
                <button type="button" className="btn btn-primary btn-sm pt-upload-btn" onClick={requestUpload}>
                    <UploadIcon className="toolbar-icon" /> <span className="pt-upload-label">Upload</span>
                </button>

                <NotificationBell backLabel={pageTitle(route)} />

                <Menu
                    align="right"
                    renderTrigger={(toggle) => (
                        <button type="button" className="mock-avatar pt-avatar-btn" onClick={toggle} aria-label="Account">{initials}</button>
                    )}
                >
                    {(close) => (
                        <div className="pt-account-menu">
                            <div className="pt-account-head"><b>{accountName}</b>{accountEmail && <span>{accountEmail}</span>}</div>
                            <button type="button" onClick={() => { navigate('sharing'); close(); }}>Library and sharing</button>
                            <button type="button" onClick={() => { navigate('trash'); close(); }}>Recently deleted</button>
                            <button type="button" onClick={() => { navigate('tools'); close(); }}>Tools</button>
                            <button type="button" onClick={() => { navigate('additional'); close(); }}>About Keepsake</button>
                            <LibrarySwitcher onSwitched={close} />
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
                            <button type="button" onClick={() => { close(); onSignOut(); }}>Sign out</button>
                        </div>
                    )}
                </Menu>
            </div>
        </header>
    );
};

const MobileTabbar: React.FC = () => {
    const { route, navigate, selection, selectMode } = useStore();
    const active = activeMobileNavFor(route.page);
    // While selecting, the selection's action bar takes this bar's place.
    if (selection.length || (selectMode && (route.page === 'gallery' || route.page === 'albums'))) return null;
    return (
        <nav className="mock-tabbar" aria-label="Primary navigation (mobile)">
            {MOBILE_NAV.map((item) => {
                const isActive = item.id === active;
                const [Outline, Solid] = MOBILE_NAV_ICONS[item.id];
                const Icon = isActive ? Solid : Outline;
                return (
                    <button key={item.id} type="button" className={isActive ? 'on' : undefined} aria-current={isActive ? 'page' : undefined} onClick={() => navigate(item.id)}>
                        <Icon className="mock-tabbar-icon" aria-hidden="true" strokeWidth={isActive ? 2.5 : 2} />
                        <span>{item.label}</span>
                    </button>
                );
            })}
        </nav>
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
    const snapshot = useHomeSnapshot();
    if (libraryIndexReady !== true) {
        // A return visit shows the last gallery from this device while the backend wakes.
        if (snapshot === undefined) return null;
        return snapshot ? <CachedGalleryPreview snapshot={snapshot} /> : <Loading label="Loading Keepsake…" />;
    }
    return (
        <StoreProvider>
            <Shell onSignOut={onSignOut} />
            <PreviewUntilGallery snapshot={snapshot} />
            <HomeSnapshotSaver />
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
            if (change.entityIds?.length) {
                if (change.operation === 'photos-deleted') removeLocalSortIndexRows(change.entityIds);
                if (change.operation === 'photos-restored') clearLocalSortIndexRemovals(change.entityIds);
            }
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
    const [theme, setTheme] = useState<Theme>(loadTheme);
    useEffect(() => applyTheme(theme), [theme]);
    const chooseTheme = useCallback((next: Theme) => {
        setTheme(next);
        saveTheme(next);
    }, []);

    return (
        <div className="pt-shell">
            <div className="mock-stage">
                <div className="mock-viewport">
                    <div className="mock-app">
                        <AppSidebar />
                        <Topbar theme={theme} onTheme={chooseTheme} onSignOut={onSignOut} />
                        <div className="mock-body pt-body">
                            <IndexBuildBar />
                            <DataRefreshBridge />
                            <Page />
                        </div>
                        <MobileTabbar />
                        <WidgetStackProvider>
                            <UploadDock />
                            <ProcessingDock />
                        </WidgetStackProvider>
                        <ScrollToTopButton />
                        <StoreFloatingSelectButton />
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
        // signOut() wipes the token + this account's cached library data
        // (see clientDataReset.ts). Reload rather than just refreshing auth
        // state so the in-memory store drops the signed-out account's photos
        // too -- otherwise they linger until the next login reboots the app.
        if (typeof window !== 'undefined' && isAuthEnabled()) {
            window.location.assign('/login');
            return;
        }
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
        <>
            <AppServicesProvider>
                <AppShellGate onSignOut={handleSignOut} />
            </AppServicesProvider>
            {/* A library-clean link from the approval email opens as a dialog over the app. It sits
                outside AppShellGate so it only waits for its own status call, not the library index. */}
            <React.Suspense fallback={null}><LazyLibraryCleanApprovalDialog /></React.Suspense>
        </>
    );
};

export default PrototypeApp;
