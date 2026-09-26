import React, { useCallback, useEffect, useState } from 'react';
import { MemoryRouter, BrowserRouter, Routes, Route } from 'react-router-dom';
import { PlusIcon } from '@heroicons/react/24/outline';
import { LogoLockup } from '../../components/shared/Logo';
import Loading from '../../components/shared/Loading';
import { AppServicesProvider, useAppServices } from '../../components/AppServicesProvider';
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

const NAV: { id: PageId; label: string }[] = [
    { id: 'ask', label: 'Ask' },
    { id: 'gallery', label: 'Gallery' },
    { id: 'albums', label: 'Albums' },
    { id: 'people', label: 'People' },
    { id: 'explore', label: 'Explore' },
    { id: 'tools', label: 'Tools' },
    { id: 'sharing', label: 'Sharing' },
];
const MOBILE_NAV = NAV.slice(0, 5);

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
            <button type="button" className="pt-wordmark-btn" onClick={() => navigate('gallery')} aria-label="Home">
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
                            <button type="button" onClick={() => { toast('Corrupted uploads: all clear'); close(); }}>Corrupted uploads</button>
                            <button type="button" onClick={() => { navigate('additional'); close(); }}>Additional info</button>
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
    const { route, navigate } = useStore();
    const active = activeNavFor(route.page);
    return (
        <nav className="mock-tabbar" aria-label="Primary navigation (mobile)">
            {MOBILE_NAV.map((item) => (
                <button key={item.id} type="button" className={item.id === active ? 'on' : undefined} onClick={() => navigate(item.id)}>
                    {item.label}
                </button>
            ))}
        </nav>
    );
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
            <StoreProvider>
                <Shell onSignOut={handleSignOut} />
            </StoreProvider>
        </AppServicesProvider>
    );
};

export default PrototypeApp;
