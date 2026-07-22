import React, { useEffect, useLayoutEffect, useState } from 'react';
import { BrowserRouter as Router, Link, NavLink, Navigate, Routes, Route, useLocation } from 'react-router-dom';
import {
    ArrowPathIcon,
    ArrowLeftOnRectangleIcon,
    ArrowRightOnRectangleIcon,
    ComputerDesktopIcon,
    CpuChipIcon,
    KeyIcon,
    MoonIcon,
    PlusIcon,
    SunIcon,
    XMarkIcon,
} from '@heroicons/react/24/outline';
import { AppServicesProvider, NotificationBell, useAppServices } from './components/AppServicesProvider';
import { getActiveAccount, initAuth, isAuthEnabled, signIn, signOut } from './services/authClient';
import { getRuntimeConfig } from './config/appConfig';

const isPasswordMode = (): boolean => (getRuntimeConfig().authMode || '').toLowerCase() === 'password';

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
const THEME_OPTIONS: Array<{ value: ThemePreference; label: string }> = [
    { value: 'light', label: 'Day' },
    { value: 'system', label: 'System' },
    { value: 'dark', label: 'Night' },
];

const isThemePreference = (value: string | null): value is ThemePreference => (
    value === 'system' || value === 'light' || value === 'dark'
);

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

    return (
        <div className="root-service-actions" aria-label="Library actions">
            <button
                type="button"
                onClick={appServices.requestUpload}
                className="btn btn-primary icon-btn"
                disabled={appServices.uploading}
                aria-label={appServices.pendingUploadSummary ? 'Reselect upload files' : appServices.uploading ? 'Uploading' : 'Upload'}
                title={appServices.pendingUploadSummary ? 'Reselect upload files' : appServices.uploading ? 'Uploading' : 'Upload'}
            >
                <PlusIcon className="toolbar-icon" />
                <span className="sr-only">{appServices.pendingUploadSummary ? 'Reselect upload files' : appServices.uploading ? 'Uploading' : 'Upload'}</span>
            </button>

            <button
                type="button"
                onClick={() => void appServices.loadBrowserAiModel()}
                className={appServices.browserAiButtonClass}
                disabled={appServices.browserAiButtonDisabled}
                aria-label={appServices.browserAiButtonLabel}
                title={appServices.browserAiButtonLabel}
            >
                {appServices.browserAiModelState.status === 'loading' || appServices.browserAiModelState.status === 'checking' ? (
                    <ArrowPathIcon className="toolbar-icon browser-ai-model-spinner" />
                ) : (
                    <CpuChipIcon className="toolbar-icon" />
                )}
                <span className="sr-only">{appServices.browserAiButtonLabel}</span>
            </button>

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

            <NotificationBell />
        </div>
    );
};

const AppContent: React.FC = () => {
    const location = useLocation();
    const appServices = useAppServices();
    const authEnabled = isAuthEnabled();
    const isPublicAlbumRoute = location.pathname.startsWith('/public/album/');
    const isAuthRoute = location.pathname === '/login' || location.pathname === '/logout' || location.pathname === '/reset-password' || location.pathname === '/change-password';
    const [authReady, setAuthReady] = useState<boolean>(false);
    const [displayName, setDisplayName] = useState<string>('');
    const [themePreference, setThemePreference] = useState<ThemePreference>(getStoredThemePreference);
    const isPrivateArea = !isPublicAlbumRoute && !isAuthRoute;
    const isSignedIntoPrivateArea = isPrivateArea && (!authEnabled || (authReady && Boolean(displayName)));

    const renderLazyPage = (element: JSX.Element, fallback = 'Loading...') => (
        <React.Suspense fallback={<p className="status">{fallback}</p>}>
            {element}
        </React.Suspense>
    );

    const renderProtectedLazyPage = (element: JSX.Element, fallback = 'Loading...') => (
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
            return <p className="status">Preparing sign-in flow...</p>;
        }
        if (!displayName) {
            return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />;
        }
        return element;
    };

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
                    <div>
                        <p className="ios-kicker">PHOTO STORE</p>
                        <h1 className="ios-title">Library</h1>
                        <p className="ios-subtitle">An elegant home for your memories.</p>
                    </div>
                    <div className="app-header-actions">
                        <ThemeSwitcher
                            preference={themePreference}
                            onPreferenceChange={setThemePreference}
                        />
                        {isSignedIntoPrivateArea && <RootServiceActions />}
                        {authEnabled && authReady && !isAuthRoute && (
                            <div className="auth-actions">
                                {displayName ? (
                                    <>
                                        <span className="auth-user">{displayName}</span>
                                        {isPasswordMode() && (
                                            <Link
                                                to="/change-password"
                                                className="btn btn-soft icon-btn"
                                                aria-label="Change password"
                                            >
                                                <KeyIcon className="toolbar-icon" />
                                                <span className="sr-only">Change password</span>
                                            </Link>
                                        )}
                                        <button
                                            type="button"
                                            className="btn btn-soft icon-btn"
                                            onClick={handleSignOut}
                                            aria-label="Sign out"
                                        >
                                            <ArrowLeftOnRectangleIcon className="toolbar-icon" />
                                            <span className="sr-only">Sign out</span>
                                        </button>
                                    </>
                                ) : (
                                    <button
                                        type="button"
                                        className="btn btn-primary icon-btn"
                                        onClick={handleSignIn}
                                        aria-label="Sign in"
                                    >
                                        <ArrowRightOnRectangleIcon className="toolbar-icon" />
                                        <span className="sr-only">Sign in</span>
                                    </button>
                                )}
                            </div>
                        )}
                    </div>
                </header>
                )}

                {!isPublicAlbumRoute && (
                <nav className="ios-tabs reveal-up delay-1" aria-label="Primary navigation">
                    <NavLink
                        to="/"
                        end
                        className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                    >
                        Gallery
                    </NavLink>
                    <NavLink
                        to="/albums"
                        className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                    >
                        Albums
                    </NavLink>
                    <NavLink
                        to="/tools"
                        className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                    >
                        Tools
                    </NavLink>
                    <NavLink
                        to="/people"
                        className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                    >
                        People
                    </NavLink>
                    <span className="ios-tab-spacer" aria-hidden="true" />
                    <NavLink
                        to="/corrupted"
                        className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                    >
                        Corrupted
                    </NavLink>
                    <NavLink
                        to="/additional"
                        className={({ isActive }) => `ios-tab${isActive ? ' active' : ''}`}
                    >
                        Additional Info
                    </NavLink>
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
                                    'Loading library...',
                                ),
                            },
                            { path: '/albums', element: renderProtectedLazyPage(<LazyAlbumsPage />, 'Loading albums...') },
                            { path: '/tools/*', element: renderProtectedLazyPage(<LazyToolsPage />, 'Loading tools...') },
                            {
                                path: '/corrupted',
                                element: renderProtectedLazyPage(<LazyCorruptedUploadsPage />, 'Loading corrupted uploads...'),
                            },
                            {
                                path: '/additional',
                                element: renderProtectedLazyPage(<LazyAdditionalInfoPage />, 'Loading additional info...'),
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
                                        'Loading sign in...',
                                    )
                                )
                            }
                        />
                        <Route
                            path="/reset-password"
                            element={renderLazyPage(<LazyResetPasswordPage />, 'Loading reset...')}
                        />
                        <Route
                            path="/change-password"
                            element={renderLazyPage(<LazyChangePasswordPage />, 'Loading...')}
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
                                'Loading sign out...',
                            )}
                        />
                        <Route
                            path="/public/album/:token"
                            element={renderLazyPage(<LazyPublicAlbumPage />, 'Loading public album...')}
                        />
                        <Route path="/faces" element={<Navigate to="/people" replace />} />
                        <Route path="/people" element={renderLazyPage(<LazyPeoplePage />, 'Loading people...')} />
                        <Route path="/people/:personId" element={renderLazyPage(<LazyPersonDetail />, 'Loading person details...')} />
                    </Routes>
                </main>
            </div>
        </div>
    );
};

const App = () => (
    <AppServicesProvider>
        <Router>
            <AppContent />
        </Router>
    </AppServicesProvider>
);

export default App;
