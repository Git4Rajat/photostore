import { AccountInfo, AuthenticationResult, PublicClientApplication } from '@azure/msal-browser';
import { getRuntimeConfig } from '../config/appConfig';
import * as passwordAuth from './passwordAuthClient';

const env = import.meta.env as Record<string, string | undefined>;
const runtimeConfig = getRuntimeConfig();

// 'password' = single-owner email/password auth; anything else falls back to
// the Microsoft Entra (MSAL) flow below.
const isPasswordMode = (runtimeConfig.authMode || '').toLowerCase() === 'password';

const tenantId = runtimeConfig.azureAdTenantId || env.VITE_AZURE_AD_TENANT_ID || env.REACT_APP_AZURE_AD_TENANT_ID || '';
const clientId = runtimeConfig.azureAdClientId || env.VITE_AZURE_AD_CLIENT_ID || env.REACT_APP_AZURE_AD_CLIENT_ID || '';
const apiScope = runtimeConfig.azureAdApiScope || env.VITE_AZURE_AD_API_SCOPE || env.REACT_APP_AZURE_AD_API_SCOPE || (clientId ? `api://${clientId}/access_as_user` : '');

const enabled = Boolean(tenantId && clientId && apiScope);

// A minimal AccountInfo-shaped object for password mode (only name/username are read).
const passwordAccount = (): AccountInfo | null => {
    if (!passwordAuth.hasValidSession()) {
        return null;
    }
    const email = passwordAuth.getEmailFromToken() || 'Owner';
    return { name: email, username: email } as AccountInfo;
};

let msalApp: PublicClientApplication | null = null;
let initialized = false;

const getRedirectUri = (): string => {
    if (typeof window === 'undefined') {
        return '';
    }
    return runtimeConfig.spaBaseUrl || window.location.origin;
};

const getAuthority = (): string => `https://login.microsoftonline.com/${tenantId}`;

export const isAuthEnabled = (): boolean => isPasswordMode || enabled;

export const initAuth = async (): Promise<void> => {
    if (isPasswordMode) {
        return; // nothing to bootstrap; session lives in localStorage
    }
    if (!enabled || initialized) {
        return;
    }

    msalApp = new PublicClientApplication({
        auth: {
            clientId,
            authority: getAuthority(),
            redirectUri: getRedirectUri(),
        },
        cache: {
            cacheLocation: 'sessionStorage',
        },
    });

    await msalApp.initialize();
    const redirectResult = await msalApp.handleRedirectPromise();
    if (redirectResult?.account) {
        msalApp.setActiveAccount(redirectResult.account);
    }
    initialized = true;
};

const ensureInitialized = async (): Promise<void> => {
    if (!enabled) {
        return;
    }
    if (!initialized) {
        await initAuth();
    }
};

export const getActiveAccount = (): AccountInfo | null => {
    if (isPasswordMode) {
        return passwordAccount();
    }
    if (!enabled || !msalApp) {
        return null;
    }
    const account = msalApp.getActiveAccount();
    if (account) {
        return account;
    }
    const all = msalApp.getAllAccounts();
    if (all.length > 0) {
        msalApp.setActiveAccount(all[0]);
        return all[0];
    }
    return null;
};

export const signIn = async (): Promise<void> => {
    if (isPasswordMode) {
        return; // the password login form drives sign-in, not a redirect
    }
    if (!enabled) {
        return;
    }
    await ensureInitialized();
    if (!msalApp) {
        return;
    }

    await msalApp.loginRedirect({
        scopes: ['openid', 'profile', apiScope],
        prompt: 'select_account',
    });
};

export const signOut = async (): Promise<void> => {
    if (isPasswordMode) {
        passwordAuth.logout();
        return;
    }
    if (!enabled || !msalApp) {
        return;
    }
    // Drop the exchanged Photostore session token too, so a later sign-in mints a
    // fresh one rather than reusing a stale active-library.
    passwordAuth.clearToken();
    const account = getActiveAccount();
    await msalApp.logoutRedirect({ account: account || undefined });
};

const apiBase = (): string => (runtimeConfig.apiBaseUrl || '').replace(/\/$/, '');

// Entra mode: exchange the Microsoft access token for a Photostore session
// token that carries the active library + token version. The backend resolver
// validates OUR token (not Microsoft's), so every API call must send this.
const exchangeEntraToken = async (msToken: string): Promise<string | null> => {
    const response = await fetch(`${apiBase()}/auth/exchange`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${msToken}` },
    });
    if (!response.ok) {
        return null;
    }
    const body = await response.json();
    const token = body?.token ? String(body.token) : '';
    if (!token) {
        return null;
    }
    passwordAuth.setSessionToken(token);
    return token;
};

export const getAccessToken = async (): Promise<string | null> => {
    // Both modes attach the Photostore-issued session token. In Entra mode we
    // first obtain it by exchanging a freshly-acquired Microsoft access token.
    if (isPasswordMode) {
        return passwordAuth.getToken();
    }
    if (!enabled) {
        return null;
    }

    await ensureInitialized();
    if (!msalApp) {
        return null;
    }

    const account = getActiveAccount();
    if (!account) {
        return null;
    }

    // Reuse a still-valid exchanged session token ONLY if it belongs to the
    // account currently signed in — otherwise switching Microsoft accounts (e.g.
    // "Use a different account") without signing out would keep acting as, and
    // exposing the library of, the previous user.
    if (passwordAuth.hasValidSession() && passwordAuth.getSubjectFromToken() === account.localAccountId) {
        return passwordAuth.getToken();
    }

    try {
        const result: AuthenticationResult = await msalApp.acquireTokenSilent({
            account,
            scopes: [apiScope],
        });
        return await exchangeEntraToken(result.accessToken);
    } catch {
        await msalApp.acquireTokenRedirect({
            account,
            scopes: [apiScope],
        });
        return null;
    }
};
