// Client for the shared-library endpoints (/api/library/*).
//
// Several endpoints (switch, leave, accept, change-password) re-issue the
// Photostore session token; whenever a response carries a `token` we persist it
// via passwordAuth.setSessionToken so subsequent calls use the new active
// library / token version.

import { getRuntimeConfig } from '../config/appConfig';
import { getAccessToken } from './authClient';
import * as passwordAuth from './passwordAuthClient';

const apiBase = (): string => (getRuntimeConfig().apiBaseUrl || '').replace(/\/$/, '');
const url = (path: string): string => `${apiBase()}${path}`;

export interface LibrarySummary {
    libraryId: string;
    name: string;
    ownerUserId: string;
    isOwner: boolean;
}

export interface LibraryMember {
    userId: string;
    email: string;
    isOwner: boolean;
    isSelf: boolean;
}

export interface PendingInvite {
    inviteId: string;
    email: string;
    targetType: string;
    expiresAt: number;
}

export interface MembersResponse {
    libraryId: string;
    name: string;
    ownerUserId: string;
    isOwner: boolean;
    members: LibraryMember[];
    maxMembers: number;
    pendingInvites?: PendingInvite[];
}

export interface MineResponse {
    activeLibraryId: string;
    libraries: LibrarySummary[];
    maxMembers: number;
}

export interface InviteInfo {
    valid: boolean;
    email?: string;
    targetType?: string;
    libraryName?: string;
    accountExists?: boolean;
    needsPassword?: boolean;
}

const parseError = async (response: Response, fallback: string): Promise<string> => {
    try {
        const body = await response.json();
        return body?.error || fallback;
    } catch {
        return fallback;
    }
};

// Attach the Bearer token when a session exists. `requireAuth: false` lets the
// unauthenticated invite flows (info / new-account accept) proceed without one.
const authedFetch = async (
    path: string,
    init: RequestInit = {},
    requireAuth = true,
): Promise<Response> => {
    const token = await getAccessToken();
    if (!token && requireAuth) {
        throw new Error('You are not signed in.');
    }
    const headers: Record<string, string> = {
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...((init.headers as Record<string, string>) || {}),
    };
    if (token) {
        headers.Authorization = `Bearer ${token}`;
    }
    return fetch(url(path), { ...init, headers });
};

// If a response re-issued a session token, persist it before returning the body.
const storeTokenIfPresent = (body: unknown): void => {
    const token = (body as { token?: string } | null)?.token;
    if (token) {
        passwordAuth.setSessionToken(String(token));
    }
};

export const getMine = async (): Promise<MineResponse> => {
    const response = await authedFetch('/api/library/mine');
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not load your libraries.'));
    }
    return response.json();
};

export const getMembers = async (): Promise<MembersResponse> => {
    const response = await authedFetch('/api/library/members');
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not load members.'));
    }
    return response.json();
};

export const sendInvite = async (email: string, targetType: 'join' | 'fresh'): Promise<void> => {
    const response = await authedFetch('/api/library/invite', {
        method: 'POST',
        body: JSON.stringify({ email, targetType }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not send the invitation.'));
    }
};

export const revokePendingInvite = async (inviteId: string): Promise<void> => {
    const response = await authedFetch('/api/library/invite/revoke', {
        method: 'POST',
        body: JSON.stringify({ inviteId }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not revoke that invitation.'));
    }
};

export const getInviteInfo = async (token: string): Promise<InviteInfo> => {
    const response = await authedFetch(
        `/api/library/invite/info?token=${encodeURIComponent(token)}`,
        {},
        false,
    );
    if (!response.ok) {
        return { valid: false };
    }
    return response.json();
};

export const acceptInvite = async (
    token: string,
    password?: string,
): Promise<{ activeLibraryId: string; newAccount: boolean }> => {
    const response = await authedFetch(
        '/api/library/invite/accept',
        { method: 'POST', body: JSON.stringify(password ? { token, password } : { token }) },
        false,
    );
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not accept this invitation.'));
    }
    const body = await response.json();
    storeTokenIfPresent(body);
    return { activeLibraryId: String(body.activeLibraryId || ''), newAccount: Boolean(body.newAccount) };
};

export const switchLibrary = async (libraryId: string): Promise<string> => {
    const response = await authedFetch('/api/library/switch', {
        method: 'POST',
        body: JSON.stringify({ libraryId }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not switch libraries.'));
    }
    const body = await response.json();
    storeTokenIfPresent(body);
    return String(body.activeLibraryId || libraryId);
};

export const renameLibrary = async (name: string): Promise<void> => {
    const response = await authedFetch('/api/library/rename', {
        method: 'POST',
        body: JSON.stringify({ name }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not rename the library.'));
    }
};

export const removeMember = async (userId: string): Promise<void> => {
    const response = await authedFetch('/api/library/members/remove', {
        method: 'POST',
        body: JSON.stringify({ userId }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not remove that member.'));
    }
};

export const leaveLibrary = async (libraryId: string): Promise<string> => {
    const response = await authedFetch('/api/library/leave', {
        method: 'POST',
        body: JSON.stringify({ libraryId }),
    });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not leave that library.'));
    }
    const body = await response.json();
    storeTokenIfPresent(body);
    return String(body.activeLibraryId || '');
};

export const deleteLibrary = async (): Promise<void> => {
    const response = await authedFetch('/api/library', { method: 'DELETE' });
    if (!response.ok) {
        throw new Error(await parseError(response, 'Could not delete the library.'));
    }
};
