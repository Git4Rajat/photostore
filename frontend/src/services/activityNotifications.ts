import { get, post } from './apiClient';
import type { AppNotification } from '../types/browserProcessing';

// What the bell learns from the server (backend/routes/activity.py and
// /api/upload-sessions/<id>/processing): shared albums someone opened, invites
// about to end, and how a finished upload's processing went. Each server fact
// becomes one notification; a small per-library record of what was already
// announced keeps a cleared bell from filling up with the same news again.

export interface AlbumOpen { albumId: string; name: string; openedAt: number; opens: number }
export interface InviteEnding { inviteId: string; email: string; expiresAt: number }
export interface Activity { albumOpens?: AlbumOpen[]; invitesEnding?: InviteEnding[] }
export interface AnnouncedActivity { opens: Record<string, number>; invites: string[] }

export interface ProcessingSummary {
    done: boolean;
    cancelled?: boolean;
    processedSeconds?: number;
    failed?: number;
    people?: number | null;
}

const REQUEST = { singleAttempt: true, timeout: 15000 };

export const fetchActivity = () => get<Activity>('/api/activity', REQUEST);
export const fetchProcessingSummary = (sessionId: string) => get<ProcessingSummary>(
    `/api/upload-sessions/${encodeURIComponent(sessionId)}/processing`, REQUEST);
export const resendInvite = (inviteId: string) => post('/api/library/invite/resend', { inviteId }, REQUEST);

const STORAGE_KEY = 'photostore.announcedActivity';

export const loadAnnouncedActivity = (scope: string): AnnouncedActivity => {
    try {
        const parsed = JSON.parse(localStorage.getItem(`${STORAGE_KEY}.${scope}`) || 'null');
        if (parsed && typeof parsed === 'object') {
            return { opens: parsed.opens && typeof parsed.opens === 'object' ? parsed.opens : {}, invites: Array.isArray(parsed.invites) ? parsed.invites : [] };
        }
    } catch {
        // Unreadable storage: announce again rather than never.
    }
    return { opens: {}, invites: [] };
};

export const saveAnnouncedActivity = (scope: string, announced: AnnouncedActivity): void => {
    try {
        localStorage.setItem(`${STORAGE_KEY}.${scope}`, JSON.stringify(announced));
    } catch {
        // Quota or private mode: the worst case is one repeated entry.
    }
};

const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString();
const clock = (date: Date) => date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });

/** New notifications for what the server reports, and the record of what is now announced.
 *  Albums and invites the server no longer lists drop out of the record. */
export const activityNotifications = (
    activity: Activity, announced: AnnouncedActivity, now = Date.now(),
): { entries: AppNotification[]; announced: AnnouncedActivity } => {
    const entries: AppNotification[] = [];
    const opens: Record<string, number> = {};
    for (const open of activity.albumOpens || []) {
        const before = announced.opens[open.albumId] ?? 0;
        opens[open.albumId] = Math.max(before, open.opens);
        const fresh = open.opens - before;
        if (fresh <= 0) continue;
        const name = `“${open.name || 'Untitled album'}”`;
        entries.push({
            id: `album-open:${open.albumId}:${open.opens}`,
            kind: 'share',
            title: fresh === 1 ? `Someone opened ${name}` : `${name} was opened ${fresh} times`,
            details: '',
            timestamp: open.openedAt * 1000,
            unread: true,
        });
    }
    const invites: string[] = [];
    for (const invite of activity.invitesEnding || []) {
        invites.push(invite.inviteId);
        if (announced.invites.includes(invite.inviteId)) continue;
        const ends = new Date(invite.expiresAt * 1000);
        entries.push({
            id: `invite-ending:${invite.inviteId}`,
            kind: 'invite',
            inviteId: invite.inviteId,
            title: `Invite to ${invite.email} ends ${sameDay(ends, new Date(now)) ? 'today' : 'tomorrow'}`,
            details: `The link stops working at ${clock(ends)}.`,
            timestamp: now,
            unread: true,
        });
    }
    return { entries, announced: { opens, invites } };
};

const duration = (seconds: number): string => {
    if (seconds < 60) return 'under a minute';
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes === 1 ? '1 minute' : `${minutes} minutes`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return `${hours === 1 ? '1 hour' : `${hours} hours`}${rest ? ` ${rest === 1 ? '1 minute' : `${rest} minutes`}` : ''}`;
};

/** "All processed in 4 minutes. 9 people found." for a finished upload, as the redesign words it. */
export const processingSummaryText = (summary: ProcessingSummary): string => {
    const seconds = summary.processedSeconds ?? 0;
    const failed = summary.failed ?? 0;
    const first = failed > 0
        ? `Processed in ${duration(seconds)}; ${failed === 1 ? '1 couldn’t' : `${failed} couldn’t`} be processed.`
        : `All processed in ${duration(seconds)}.`;
    const people = summary.people;
    if (people === null || people === undefined || people <= 0) return first;
    return `${first} ${people === 1 ? '1 person' : `${people} people`} found.`;
};
