import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { BrowserRouter as Router, Link, NavLink, Navigate, Routes, Route, useLocation } from 'react-router-dom';
import {
    ArrowPathIcon,
    ArrowLeftOnRectangleIcon,
    ArrowRightOnRectangleIcon,
    Bars3Icon,
    ChevronDownIcon,
    ComputerDesktopIcon,
    CpuChipIcon,
    EllipsisHorizontalIcon,
    ExclamationTriangleIcon,
    InformationCircleIcon,
    KeyIcon,
    MoonIcon,
    PhotoIcon,
    PlusIcon,
    RectangleStackIcon,
    RocketLaunchIcon,
    ShareIcon,
    SunIcon,
    UserCircleIcon,
    UsersIcon,
    WrenchScrewdriverIcon,
    XMarkIcon,
} from '@heroicons/react/24/outline';
import { AppServicesProvider, ClusteringActivityIndicator, NotificationBell, useAppServices, getBrowserProcessingConcurrency, isBrowserProcessingTurboEnabled, setBrowserProcessingTurbo } from './components/AppServicesProvider';
import { TimelineMetadataProvider } from './components/TimelineMetadataProvider';
import { Logo } from './components/shared/Logo';
import { Loading } from './components/shared/Loading';
import { BackendStatusBanner } from './components/shared/BackendStatusBanner';
import NotFoundPage from './components/NotFoundPage';
import { DialogHost } from './components/shared/dialogs';
import { getActiveAccount, initAuth, isAuthEnabled, signIn, signOut } from './services/authClient';
import { getMine } from './services/libraryClient';
import { getRuntimeConfig } from './config/appConfig';

const isPasswordMode = (): boolean => (getRuntimeConfig().authMode || '').toLowerCase() === 'password';

const APP_NAME = 'Keepsake';

// Maps the current route to a human page name for the browser tab title, so
// tabs and bookmarks read "Gallery · Keepsake" instead of a bare "Keepsake".
const pageTitleFor = (pathname: string): string => {
    if (pathname === '/') return 'Gallery';
    if (pathname.startsWith('/albums')) return 'Albums';
    if (pathname.startsWith('/tools')) return 'Tools';
    if (pathname.startsWith('/people')) return 'People';
    if (pathname.startsWith('/library')) return 'Sharing';
    if (pathname.startsWith('/corrupted')) return 'Corrupted uploads';
    if (pathname.startsWith('/additional')) return 'Additional info';
    if (pathname === '/login') return 'Sign in';
    if (pathname === '/logout') return 'Sign out';
    if (pathname === '/reset-password') return 'Reset password';
    if (pathname === '/change-password') return 'Change password';
    if (pathname === '/accept-invite') return 'Accept invitation';
    if (pathname === '/confirm-library-clean') return 'Confirm library cleanup';
    if (pathname.startsWith('/public/album/')) return 'Shared album';
    return '';
};

const loadPhotoGalleryPage = () => import('./components/PhotoGallery');
const loadAlbumsPage = () => import('./components/AlbumsPage');
const loadToolsPage = () => import('./components/ToolsPage');
const loadPeoplePage = () => import('./components/PeoplePage');

const LazyPhotoGallery = React.lazy(loadPhotoGalleryPage);
const LazyAlbumsPage = React.lazy(loadAlbumsPage);
const LazyToolsPage = React.lazy(loadToolsPage);
const LazyPeoplePage = React.lazy(loadPeoplePage);
const LazyPersonDetail = React.lazy(() => import('./components/PersonDetail'));
const LazyAdditionalInfoPage = React.lazy(() => import('./components/AdditionalInfoPage'));
const LazyCorruptedUploadsPage = React.lazy(() => import('./components/CorruptedUploadsPage'));
const LazyPublicAlbumPage = React.lazy(() => import('./components/PublicAlbumPage'));
const LazyLoginPage = React.lazy(() => import('./components/LoginPage'));
const LazyLogoutPage = React.lazy(() => import('./components/LogoutPage'));
const LazyResetPasswordPage = React.lazy(() => import('./components/ResetPasswordPage'));
const LazyChangePasswordPage = React.lazy(() => import('./components/ChangePasswordPage'));
const LazyAcceptInvitePage = React.lazy(() => import('./components/AcceptInvitePage'));
const LazyConfirmLibraryCleanPage = React.lazy(() => import('./components/ConfirmLibraryCleanPage'));
const LazyLibraryPage = React.lazy(() => import('./components/LibraryPage'));

const PRIVATE_TAB_PRELOADERS = [
    loadPhotoGalleryPage,
    loadAlbumsPage,
    loadToolsPage,
    loadPeoplePage,
];

let privateTabPreloadStarted = false;

const preloadPrivateTabPages = () => {
    if (privateTabPreloadStarted) {
        return;
    }
    privateTabPreloadStarted = true;
    void Promise.allSettled(PRIVATE_TAB_PRELOADERS.map((loadPage) => loadPage()));
};

type ThemePreference = 'system' | 'light' | 'dark';
type ResolvedTheme = 'light' | 'dark';

const THEME_STORAGE_KEY = 'photostore-theme-preference';
const BACKGROUND_TAB_WARNING_DISMISSED_KEY = 'photostore-background-tab-warning-dismissed';
const THEME_OPTIONS: Array<{ value: ThemePreference; label: string }> = [
    { value: 'light', label: 'Day' },
    { value: 'system', label: 'System' },
    { value: 'dark', label: 'Night' },
];

const isThemePreference = (value: string | null): value is ThemePreference => (
    value === 'system' || value === 'light' || value === 'dark'
);

interface NavItem {
    to: string;
    label: string;
    end?: boolean;
    Icon: React.FC<React.SVGProps<SVGSVGElement>>;
}

// Primary destinations, surfaced as the desktop tab bar and the top group of the
// mobile navigation drawer.
const PRIMARY_NAV_ITEMS: NavItem[] = [
    { to: '/', label: 'Gallery', end: true, Icon: PhotoIcon },
    { to: '/albums', label: 'Albums', Icon: RectangleStackIcon },
    { to: '/tools', label: 'Tools', Icon: WrenchScrewdriverIcon },
    { to: '/people', label: 'People', Icon: UsersIcon },
    { to: '/library', label: 'Sharing', Icon: ShareIcon },
];

// Rarely-used utilities, kept out of the way (right of the tab-bar spacer on
// desktop, a separate group in the drawer on mobile).
const UTILITY_NAV_ITEMS: NavItem[] = [
    { to: '/corrupted', label: 'Corrupted', Icon: ExclamationTriangleIcon },
    { to: '/additional', label: 'Additional Info', Icon: InformationCircleIcon },
];

const getSystemTheme = (): ResolvedTheme => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
        return 'light';
    }
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
};

const getStoredThemePreference = (): ThemePreference => {
    if (typeof window === 'undefined') {
        return 'system';
    }

    try {
        const storedPreference = window.localStorage.getItem(THEME_STORAGE_KEY);
        return isThemePreference(storedPreference) ? storedPreference : 'system';
    } catch {
        return 'system';
    }
};

const applyThemePreference = (preference: ThemePreference) => {
    if (typeof document === 'undefined') {
        return;
    }

    const resolvedTheme = preference === 'system' ? getSystemTheme() : preference;
    const root = document.documentElement;
    root.dataset.theme = resolvedTheme;
    root.dataset.themePreference = preference;
    root.style.colorScheme = resolvedTheme;
};

interface ThemeSwitcherProps {
    preference: ThemePreference;
    onPreferenceChange: (preference: ThemePreference) => void;
}

const ThemeSwitcher: React.FC<ThemeSwitcherProps> = ({ preference, onPreferenceChange }) => (
    <div className="theme-switcher" role="group" aria-label="Appearance mode">
        <div className="theme-options" data-preference={preference}>
            <span className="theme-slider-thumb" aria-hidden="true" />
            {THEME_OPTIONS.map((option) => (
                <button
                    key={option.value}
                    type="button"
                    className={`theme-option${preference === option.value ? ' active' : ''}`}
                    aria-label={`${option.label} appearance`}
                    aria-pressed={preference === option.value}
                    onClick={() => onPreferenceChange(option.value)}
                >
                    {option.value === 'light' && <SunIcon className="theme-option-icon" aria-hidden="true" />}
                    {option.value === 'system' && <ComputerDesktopIcon className="theme-option-icon" aria-hidden="true" />}
                    {option.value === 'dark' && <MoonIcon className="theme-option-icon" aria-hidden="true" />}
                </button>
            ))}
        </div>
    </div>
);

const RootServiceActions: React.FC = () => {
    const appServices = useAppServices();
    const location = useLocation();
    const [expanded, setExpanded] = useState<boolean>(false);
    const wrapRef = useRef<HTMLDivElement | null>(null);

    // Collapse the secondary-actions flyout whenever the route changes so it
    // never lingers open over freshly navigated content (mirrors NavDrawer).
    useEffect(() => {
        setExpanded(false);
    }, [location.pathname]);

    useEffect(() => {
        if (!expanded || typeof document === 'undefined') {
            return undefined;
        }
        const handlePointerDown = (event: MouseEvent) => {
            if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
                setExpanded(false);
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                setExpanded(false);
            }
        };
        document.addEventListener('mousedown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('mousedown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [expanded]);

    // On narrow screens the secondary icons (upload, AI toggle, stop,
    // clustering) collapse behind this chevron so the header row doesn't
    // overflow; on wider screens it's hidden and .root-service-secondary
    // renders inline as before (see index.css). Notifications stay outside
    // the toggle since their own badge already surfaces unread count.
    const needsAttention = appServices.uploading || appServices.clusteringActive;
    const uploadButtonLabel = appServices.pendingUploadSummary
        ? 'Reselect upload files'
        : appServices.uploading
            ? (appServices.queuedUploadFileCount > 0
                ? `Uploading — add more files (${appServices.queuedUploadFileCount} queued)`
                : 'Uploading — add more files')
            : 'Upload';

    return (
        <div className="root-service-actions" aria-label="Library actions" ref={wrapRef}>
            <NotificationBell />

            <button
                type="button"
                className="btn btn-soft icon-btn root-actions-toggle"
                onClick={() => setExpanded((prev) => !prev)}
                aria-expanded={expanded}
                aria-label={expanded ? 'Fewer actions' : 'More actions'}
                title={expanded ? 'Fewer actions' : 'More actions'}
            >
                <ChevronDownIcon className="toolbar-icon" aria-hidden="true" />
                {!expanded && needsAttention && <span className="root-actions-toggle-dot" aria-hidden="true" />}
                <span className="sr-only">{expanded ? 'Fewer actions' : 'More actions'}</span>
            </button>

            <div className="root-service-secondary" data-expanded={expanded}>
                <button
                    type="button"
                    onClick={appServices.requestUpload}
                    className="btn btn-primary icon-btn"
                    aria-label={uploadButtonLabel}
                    title={uploadButtonLabel}
                >
                    <PlusIcon className="toolbar-icon" />
                    <span className="sr-only">{uploadButtonLabel}</span>
                </button>

                {!appServices.clusteringActive && getRuntimeConfig().processingMode !== 'backend' && (() => {
                    const loadStage = appServices.browserAiLoadProgress;
                    const isLoading = appServices.browserAiModelState.status === 'loading' || appServices.browserAiModelState.status === 'checking';
                    const browserAiTitle = isLoading && loadStage
                        ? `${appServices.browserAiButtonLabel} — ${loadStage.label} (${loadStage.index}/${loadStage.total})`
                        : appServices.browserAiButtonLabel;
                    return (
                        <button
                            type="button"
                            onClick={() => void appServices.loadBrowserAiModel()}
                            className={appServices.browserAiButtonClass}
                            disabled={appServices.browserAiButtonDisabled}
                            aria-label={browserAiTitle}
                            title={browserAiTitle}
                        >
                            {isLoading ||
                             (appServices.browserAiModelState.status === 'available' && appServices.browserProcessingActive) ? (
                                <ArrowPathIcon className="toolbar-icon browser-ai-model-spinner" />
                            ) : (
                                <CpuChipIcon className="toolbar-icon" />
                            )}
                            {isLoading && loadStage && (
                                <span
                                    className="browser-ai-load-progress"
                                    style={{ width: `${Math.round((loadStage.index / loadStage.total) * 100)}%` }}
                                />
                            )}
                            <span className="sr-only">{browserAiTitle}</span>
                        </button>
                    );
                })()}

                {appServices.uploading && (
                    <button
                        type="button"
                        onClick={appServices.stopActiveUpload}
                        className="btn btn-danger icon-btn"
                        aria-label="Stop upload"
                        title="Stop upload"
                    >
                        <XMarkIcon className="toolbar-icon" />
                        <span className="sr-only">Stop upload</span>
                    </button>
                )}

                <ClusteringActivityIndicator />
            </div>
        </div>
    );
};

interface NavDrawerProps {
    open: boolean;
    onClose: () => void;
    libraryTitle: string;
}

// Slide-in navigation drawer used on narrow screens in place of the tab bar.
// Kept mounted so it can transition; visibility is driven by the `open` class.
const NavDrawer: React.FC<NavDrawerProps> = ({ open, onClose, libraryTitle }) => {
    useEffect(() => {
        if (!open || typeof document === 'undefined') {
            return undefined;
        }
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                onClose();
            }
        };
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [open, onClose]);

    const renderLink = ({ to, label, end, Icon }: NavItem) => (
        <NavLink
            key={to}
            to={to}
            end={end}
            onClick={onClose}
            className={({ isActive }) => `app-menu-link${isActive ? ' active' : ''}`}
        >
            <Icon className="app-menu-link-icon" aria-hidden="true" />
            <span>{label}</span>
        </NavLink>
    );

    return (
        <div className={`app-menu-drawer${open ? ' open' : ''}`} aria-hidden={!open}>
            <button
                type="button"
                className="app-menu-backdrop"
                aria-label="Close navigation menu"
                tabIndex={open ? 0 : -1}
                onClick={onClose}
            />
            <nav className="app-menu-panel" aria-label="Primary navigation">
                <div className="app-menu-head">
                    <div className="app-menu-head-text">
                        <p className="app-menu-kicker">PHOTO STORE</p>
                        <p className="app-menu-title">{libraryTitle}</p>
                    </div>
                    <button
                        type="button"
                        className="btn btn-soft icon-btn app-menu-close"
                        onClick={onClose}
                        aria-label="Close navigation menu"
                    >
                        <XMarkIcon className="toolbar-icon" />
                        <span className="sr-only">Close navigation menu</span>
                    </button>
                </div>
                <div className="app-menu-section">
                    {PRIMARY_NAV_ITEMS.map(renderLink)}
                </div>
                <p className="app-menu-section-title">Utilities</p>
                <div className="app-menu-section">
                    {UTILITY_NAV_ITEMS.map(renderLink)}
                </div>
            </nav>
        </div>
    );
};

interface AccountMenuProps {
    themePreference: ThemePreference;
    onPreferenceChange: (preference: ThemePreference) => void;
    authEnabled: boolean;
    authReady: boolean;
    isAuthRoute: boolean;
    displayName: string;
    onSignIn: () => void;
    onSignOut: () => void;
}

// Consolidates appearance + account controls behind a single overflow button so
// the header stays tidy at every width instead of wrapping into extra rows.
const AccountMenu: React.FC<AccountMenuProps> = ({
    themePreference,
    onPreferenceChange,
    authEnabled,
    authReady,
    isAuthRoute,
    displayName,
    onSignIn,
    onSignOut,
}) => {
    const [open, setOpen] = useState<boolean>(false);
    const [turbo, setTurbo] = useState<boolean>(() => isBrowserProcessingTurboEnabled());
    const wrapRef = useRef<HTMLDivElement | null>(null);
    const close = useCallback(() => setOpen(false), []);
    const turboConcurrency = getBrowserProcessingConcurrency();

    useEffect(() => {
        if (!open || typeof document === 'undefined') {
            return undefined;
        }
        const handlePointerDown = (event: MouseEvent) => {
            if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
                close();
            }
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                close();
            }
        };
        document.addEventListener('mousedown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('mousedown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [open, close]);

    const showAuth = authEnabled && authReady && !isAuthRoute;

    return (
        <div className="account-menu" ref={wrapRef}>
            <button
                type="button"
                className="btn btn-soft icon-btn account-menu-trigger"
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={() => setOpen((prev) => !prev)}
                aria-label="Settings and account"
                title="Settings and account"
            >
                <EllipsisHorizontalIcon className="toolbar-icon" />
                <span className="sr-only">Settings and account</span>
            </button>

            {open && (
                <div className="account-menu-pane" role="dialog" aria-label="Settings and account">
                    {showAuth && displayName && (
                        <div className="account-menu-identity">
                            <UserCircleIcon className="account-menu-avatar" aria-hidden="true" />
                            <span className="account-menu-user">{displayName}</span>
                        </div>
                    )}

                    <div className="account-menu-section">
                        <p className="account-menu-label">Appearance</p>
                        <ThemeSwitcher
                            preference={themePreference}
                            onPreferenceChange={onPreferenceChange}
                        />
                    </div>

                    {getRuntimeConfig().processingMode !== 'backend' && (
                        <div className="account-menu-section">
                            <p className="account-menu-label">Processing</p>
                            <button
                                type="button"
                                className={`account-menu-toggle${turbo ? ' is-on' : ''}`}
                                role="switch"
                                aria-checked={turbo}
                                onClick={() => {
                                    setTurbo((value) => {
                                        const next = !value;
                                        setBrowserProcessingTurbo(next);
                                        return next;
                                    });
                                }}
                            >
                                <span className="account-menu-toggle-copy">
                                    <span className="account-menu-toggle-title">
                                        <RocketLaunchIcon className="account-menu-item-icon" aria-hidden="true" />
                                        Turbo mode
                                    </span>
                                    <span className="account-menu-toggle-note">
                                        {turbo
                                            ? `Processes up to ${turboConcurrency} photos at once — faster, uses more CPU and memory.`
                                            : 'Processes one photo at a time — gentle on this device.'}
                                    </span>
                                </span>
                                <span className="account-menu-switch" aria-hidden="true">
                                    <span className="account-menu-switch-thumb" />
                                </span>
                            </button>
                        </div>
                    )}

                    {showAuth && (
                        <div className="account-menu-section account-menu-section-actions">
                            {displayName ? (
                                <>
                                    {isPasswordMode() && (
                                        <Link
                                            to="/change-password"
                                            className="account-menu-item"
                                            onClick={close}
                                        >
                                            <KeyIcon className="account-menu-item-icon" aria-hidden="true" />
                                            <span>Change password</span>
                                        </Link>
                                    )}
                                    <button
                                        type="button"
                                        className="account-menu-item"
                                        onClick={() => {
                                            close();
                                            onSignOut();
                                        }}
                                    >
                                        <ArrowLeftOnRectangleIcon className="account-menu-item-icon" aria-hidden="true" />
                                        <span>Sign out</span>
                                    </button>
                                </>
                            ) : (
                                <button
                                    type="button"
                                    className="account-menu-item"
                                    onClick={() => {
                                        close();
                                        onSignIn();
                                    }}
                                >
                                    <ArrowRightOnRectangleIcon className="account-menu-item-icon" aria-hidden="true" />
                                    <span>Sign in</span>
                                </button>
                            )}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
};

interface BackgroundTabWarningModalProps {
    open: boolean;
    onClose: () => void;
    onDismissForever: () => void;
}

const BackgroundTabWarningModal: React.FC<BackgroundTabWarningModalProps> = ({ open, onClose, onDismissForever }) => {
    if (!open) {
        return null;
    }

    return (
        <div className="dialog-backdrop" onClick={onClose}>
            <div className="dialog-card" onClick={(e) => e.stopPropagation()} role="dialog" aria-labelledby="background-tab-warning-title" aria-modal="true">
                <h2 id="background-tab-warning-title" className="dialog-title">Background Processing Notice</h2>
                <p className="dialog-message">
                    When this tab is not in the foreground, browser processing slows down significantly.
                    For best performance, keep this tab active while processing photos.
                </p>
                <div className="dialog-actions">
                    <button
                        type="button"
                        className="btn btn-soft"
                        onClick={onDismissForever}
                    >
                        Don't remind me again
                    </button>
                    <button
                        type="button"
                        className="btn btn-primary"
                        onClick={onClose}
                    >
                        OK
                    </button>
                </div>
            </div>
        </div>
    );
};

const AppContent: React.FC = () => {
    const location = useLocation();
    const appServices = useAppServices();
    const authEnabled = isAuthEnabled();
    const isPublicAlbumRoute = location.pathname.startsWith('/public/album/');
    const isAuthRoute = location.pathname === '/login' || location.pathname === '/logout' || location.pathname === '/reset-password' || location.pathname === '/change-password' || location.pathname === '/accept-invite' || location.pathname === '/confirm-library-clean';
    const [authReady, setAuthReady] = useState<boolean>(false);
    const [displayName, setDisplayName] = useState<string>('');
    const [libraryTitle, setLibraryTitle] = useState<string>('Library');
    const [themePreference, setThemePreference] = useState<ThemePreference>(getStoredThemePreference);
    const [menuOpen, setMenuOpen] = useState<boolean>(false);
    const [showBackgroundTabWarning, setShowBackgroundTabWarning] = useState<boolean>(false);
    const hasShownBackgroundWarningRef = useRef<boolean>(false);
    const isPrivateArea = !isPublicAlbumRoute && !isAuthRoute;
    const isSignedIntoPrivateArea = isPrivateArea && (!authEnabled || (authReady && Boolean(displayName)));

    useEffect(() => {
        const page = pageTitleFor(location.pathname);
        document.title = page ? `${page} · ${APP_NAME}` : APP_NAME;
    }, [location.pathname]);

    const renderLazyPage = (element: JSX.Element, fallback = 'Loading…') => (
        <React.Suspense fallback={<Loading label={fallback} />}>
            {element}
        </React.Suspense>
    );

    const renderProtectedLazyPage = (element: JSX.Element, fallback = 'Loading…') => (
        guardPrivateRoute(renderLazyPage(element, fallback))
    );

    const refreshAuthState = async () => {
        const account = getActiveAccount();
        if (account) {
            setDisplayName(account.name || account.username || '');
            return;
        }
        setDisplayName('');
    };

    const handleSignIn = async () => {
        await signIn();
        await refreshAuthState();
    };

    const handleSignOut = async () => {
        await signOut();
        await refreshAuthState();
    };

    const guardPrivateRoute = (element: JSX.Element) => {
        if (!authEnabled) {
            return element;
        }
        if (!authReady) {
            return <p className="status">Preparing sign-in flow…</p>;
        }
        if (!displayName) {
            return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />;
        }
        return element;
    };

    // Dismiss the mobile navigation drawer whenever the route changes so it never
    // lingers open over freshly navigated content.
    useEffect(() => {
        setMenuOpen(false);
    }, [location.pathname]);

    useLayoutEffect(() => {
        applyThemePreference(themePreference);

        try {
            window.localStorage.setItem(THEME_STORAGE_KEY, themePreference);
        } catch {
            // Theme selection should never block the app if storage is unavailable.
        }

        if (typeof window.matchMedia !== 'function' || themePreference !== 'system') {
            return undefined;
        }

        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
        const handleSystemThemeChange = () => applyThemePreference('system');
        mediaQuery.addEventListener?.('change', handleSystemThemeChange);

        return () => {
            mediaQuery.removeEventListener?.('change', handleSystemThemeChange);
        };
    }, [themePreference]);

    // Show background tab warning when tab goes to background (one-time, unless dismissed forever).
    // Only relevant to the signed-in app, which is the only place AI processing runs -- public
    // album viewers and visitors on auth pages never trigger it, so skip the listener there too.
    useEffect(() => {
        if (!isPrivateArea || typeof document === 'undefined' || typeof localStorage === 'undefined') {
            return undefined;
        }

        // Check if user has permanently dismissed this warning
        const isDismissed = localStorage.getItem(BACKGROUND_TAB_WARNING_DISMISSED_KEY) === '1';
        if (isDismissed) {
            return undefined;
        }

        const handleVisibilityChange = () => {
            // Only show warning once per session and only when tab goes to background
            if (document.hidden && !hasShownBackgroundWarningRef.current) {
                hasShownBackgroundWarningRef.current = true;
                setShowBackgroundTabWarning(true);
            }
        };

        document.addEventListener('visibilitychange', handleVisibilityChange);
        return () => {
            document.removeEventListener('visibilitychange', handleVisibilityChange);
        };
    }, [isPrivateArea]);

    const handleCloseBackgroundWarning = useCallback(() => {
        setShowBackgroundTabWarning(false);
    }, []);

    const handleDismissBackgroundWarningForever = useCallback(() => {
        setShowBackgroundTabWarning(false);
        try {
            localStorage.setItem(BACKGROUND_TAB_WARNING_DISMISSED_KEY, '1');
        } catch {
            // Ignore storage errors
        }
    }, []);

    useEffect(() => {
        let mounted = true;
        const bootstrap = async () => {
            if (!isAuthEnabled()) {
                if (mounted) {
                    setAuthReady(true);
                }
                return;
            }

            await initAuth();
            if (!mounted) {
                return;
            }
            await refreshAuthState();
            setAuthReady(true);
        };
        void bootstrap();
        return () => {
            mounted = false;
        };
    }, [authEnabled]);

    // Reflect the active library in the header title. Default personal libraries
    // are named after the owner's email, which we don't want to show, so fall
    // back to "Library" unless the owner gave the library a distinct name.
    useEffect(() => {
        if (!isSignedIntoPrivateArea) {
            return;
        }
        let cancelled = false;
        (async () => {
            try {
                const mine = await getMine();
                const active = mine.libraries.find((lib) => lib.libraryId === mine.activeLibraryId);
                const name = (active?.name || '').trim();
                const ownEmail = (getActiveAccount()?.username || '').trim().toLowerCase();
                if (!cancelled) {
                    setLibraryTitle(name && name.toLowerCase() !== ownEmail ? name : 'Library');
                }
            } catch {
                if (!cancelled) {
                    setLibraryTitle('Library');
                }
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [isSignedIntoPrivateArea, displayName]);

    useEffect(() => {
        if (!isSignedIntoPrivateArea) {
            return undefined;
        }

        let cancelled = false;
        const preloadPages = () => {
            if (!cancelled) {
                preloadPrivateTabPages();
            }
        };
        const win = window as typeof window & {
            requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
            cancelIdleCallback?: (handle: number) => void;
        };

        if (typeof win.requestIdleCallback === 'function') {
            const idleHandle = win.requestIdleCallback(preloadPages, { timeout: 2000 });
            return () => {
                cancelled = true;
                win.cancelIdleCallback?.(idleHandle);
            };
        }

        const timeoutId = window.setTimeout(preloadPages, 0);
        return () => {
            cancelled = true;
            window.clearTimeout(timeoutId);
        };
    }, [isSignedIntoPrivateArea]);

    return (
        <div className="ios-shell">
            <BackendStatusBanner />
            <div className="ios-backdrop ios-backdrop-one" />
            <div className="ios-backdrop ios-backdrop-two" />

            <div className="ios-container">
                {isPublicAlbumRoute && (
                    <div className="public-theme-row reveal-up">
                        <ThemeSwitcher
                            preference={themePreference}
                            onPreferenceChange={setThemePreference}
                        />
                    </div>
                )}

                {!isPublicAlbumRoute && (
                <header className="ios-header reveal-up">
                    {isSignedIntoPrivateArea && (
                        <button
                            type="button"
                            className="ios-menu-toggle"
                            onClick={() => setMenuOpen(true)}
                            aria-label="Open navigation menu"
                            aria-expanded={menuOpen}
                        >
                            <Bars3Icon className="toolbar-icon" />
                            <span className="sr-only">Open navigation menu</span>
                        </button>
                    )}
                    <Logo size={40} className="ios-header-logo" />
                    <div className="ios-header-title">
                        <p className="ios-kicker">KEEPSAKE</p>
                        <h1 className="ios-title">{libraryTitle}</h1>
                        <p className="ios-subtitle">An elegant home for your memories.</p>
                    </div>
                    <div className="app-header-actions">
                        {isSignedIntoPrivateArea && <RootServiceActions />}
                        <AccountMenu
                            themePreference={themePreference}
                            onPreferenceChange={setThemePreference}
                            authEnabled={authEnabled}
                            authReady={authReady}
                            isAuthRoute={isAuthRoute}
                            displayName={displayName}
                            onSignIn={handleSignIn}
                            onSignOut={handleSignOut}
                        />
                    </div>
                </header>
                )}

                {!isPublicAlbumRoute && (
                <nav className="ios-tabs reveal-up delay-1" aria-label="Primary navigation">
                    {PRIMARY_NAV_ITEMS.map(({ to, label, end }) => (
                        <NavLink
                            key={to}
                            to={to}
                            end={end}
                            className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                        >
                            {label}
                        </NavLink>
                    ))}
                    <span className="ios-tab-spacer" aria-hidden="true" />
                    {UTILITY_NAV_ITEMS.map(({ to, label, end }) => (
                        <NavLink
                            key={to}
                            to={to}
                            end={end}
                            className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                        >
                            {label}
                        </NavLink>
                    ))}
                </nav>
                )}

                {!isPublicAlbumRoute && isSignedIntoPrivateArea && appServices.pendingUploadSummary && (
                    <div className="upload-approval-bar root-upload-approval-bar reveal-up delay-1">
                        <div>
                            <p className="upload-approval-title">Upload paused</p>
                            <p className="upload-approval-details">
                                {appServices.pendingUploadSummary.fileCount} file(s) waiting
                                {appServices.pendingUploadSummary.failedCount > 0
                                    ? `, ${appServices.pendingUploadSummary.failedCount} failed`
                                    : ''}
                            </p>
                        </div>
                        <div className="upload-approval-actions">
                            <button
                                type="button"
                                className="btn btn-primary"
                                onClick={() => void appServices.retryPersistedUploadSession()}
                                disabled={appServices.uploading}
                            >
                                Retry
                            </button>
                            <button
                                type="button"
                                className="btn btn-soft"
                                onClick={() => void appServices.discardPersistedUploadSession()}
                                disabled={appServices.uploading}
                            >
                                Discard
                            </button>
                        </div>
                    </div>
                )}

                <main className="ios-main reveal-up delay-1">
                    <Routes>
                        {[
                            {
                                path: '/',
                                element: renderProtectedLazyPage(
                                    <LazyPhotoGallery
                                        addNotification={appServices.addNotification}
                                        registerUploadCompletionHandler={appServices.registerUploadCompletionHandler}
                                        registerUploadErrorHandler={appServices.registerUploadErrorHandler}
                                    />,
                                    'Loading library…',
                                ),
                            },
                            { path: '/albums', element: renderProtectedLazyPage(<LazyAlbumsPage />, 'Loading albums…') },
                            { path: '/tools/*', element: renderProtectedLazyPage(<LazyToolsPage />, 'Loading tools…') },
                            {
                                path: '/corrupted',
                                element: renderProtectedLazyPage(<LazyCorruptedUploadsPage />, 'Loading corrupted uploads…'),
                            },
                            {
                                path: '/additional',
                                element: renderProtectedLazyPage(<LazyAdditionalInfoPage />, 'Loading additional info…'),
                            },
                        ].map(({ path, element }) => (
                            <Route key={path} path={path} element={element} />
                        ))}
                        <Route
                            path="/login"
                            element={
                                authEnabled && authReady && displayName ? (
                                    <Navigate to="/" replace />
                                ) : (
                                    renderLazyPage(
                                        <LazyLoginPage
                                            authEnabled={authEnabled}
                                            authReady={authReady}
                                            displayName={displayName}
                                            onSignIn={handleSignIn}
                                            onAuthenticated={refreshAuthState}
                                        />,
                                        'Loading sign in…',
                                    )
                                )
                            }
                        />
                        <Route
                            path="/reset-password"
                            element={renderLazyPage(<LazyResetPasswordPage />, 'Loading reset…')}
                        />
                        <Route
                            path="/change-password"
                            element={renderLazyPage(<LazyChangePasswordPage />, 'Loading…')}
                        />
                        <Route
                            path="/accept-invite"
                            element={renderLazyPage(<LazyAcceptInvitePage />, 'Loading invitation…')}
                        />
                        <Route
                            path="/confirm-library-clean"
                            element={renderLazyPage(<LazyConfirmLibraryCleanPage />, 'Loading confirmation…')}
                        />
                        <Route
                            path="/library"
                            element={renderProtectedLazyPage(<LazyLibraryPage />, 'Loading library…')}
                        />
                        <Route
                            path="/logout"
                            element={renderLazyPage(
                                <LazyLogoutPage
                                    authEnabled={authEnabled}
                                    authReady={authReady}
                                    displayName={displayName}
                                    onSignOut={handleSignOut}
                                />,
                                'Loading sign out…',
                            )}
                        />
                        <Route
                            path="/public/album/:token"
                            element={renderLazyPage(<LazyPublicAlbumPage />, 'Loading public album…')}
                        />
                        <Route path="/faces" element={<Navigate to="/people" replace />} />
                        <Route path="/people" element={renderLazyPage(<LazyPeoplePage />, 'Loading people…')} />
                        <Route path="/people/:personId" element={renderLazyPage(<LazyPersonDetail />, 'Loading person details…')} />
                        <Route path="*" element={<NotFoundPage />} />
                    </Routes>
                </main>
            </div>

            {/* Rendered outside .ios-container: that element sets
                container-type: inline-size, which would otherwise become the
                containing block for this fixed drawer and break full-viewport
                positioning. */}
            {isSignedIntoPrivateArea && (
                <NavDrawer
                    open={menuOpen}
                    onClose={() => setMenuOpen(false)}
                    libraryTitle={libraryTitle}
                />
            )}

            {/* Styled confirm/prompt dialogs (replaces window.confirm/prompt). */}
            <DialogHost />

            {/* One-time background tab warning -- only applies to the signed-in app, not public links */}
            <BackgroundTabWarningModal
                open={isPrivateArea && showBackgroundTabWarning}
                onClose={handleCloseBackgroundWarning}
                onDismissForever={handleDismissBackgroundWarningForever}
            />
        </div>
    );
};

const App = () => (
    <AppServicesProvider>
        <TimelineMetadataProvider>
            <Router>
                <AppContent />
            </Router>
        </TimelineMetadataProvider>
    </AppServicesProvider>
);

export default App;
