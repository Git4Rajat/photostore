import { describe, expect, it } from 'vitest';
import { activityNotifications, processingSummaryText } from './activityNotifications';

const now = new Date(2026, 10, 17, 10, 0).getTime();

describe('activity notifications', () => {
    it('announces each new open of a shared album once', () => {
        const activity = { albumOpens: [{ albumId: 'a1', name: 'Malmö nights', openedAt: now / 1000 - 60, opens: 1 }] };
        const first = activityNotifications(activity, { opens: {}, invites: [] }, now);
        expect(first.entries).toEqual([expect.objectContaining({
            id: 'album-open:a1:1', kind: 'share', title: 'Someone opened “Malmö nights”', timestamp: now - 60_000, unread: true,
        })]);
        expect(activityNotifications(activity, first.announced, now).entries).toEqual([]);

        // Three more visits since: one entry that says so.
        const later = activityNotifications({ albumOpens: [{ ...activity.albumOpens[0], opens: 4 }] }, first.announced, now);
        expect(later.entries.map((e) => e.title)).toEqual(['“Malmö nights” was opened 3 times']);
        expect(later.announced.opens).toEqual({ a1: 4 });
    });

    it('reminds once about an invite in its last day, with Resend', () => {
        const tonight = new Date(2026, 10, 17, 21, 30).getTime() / 1000;
        const activity = { invitesEnding: [{ inviteId: 'inv1', email: 'd**@example.com', expiresAt: tonight }] };
        const first = activityNotifications(activity, { opens: {}, invites: [] }, now);
        expect(first.entries).toEqual([expect.objectContaining({
            id: 'invite-ending:inv1', kind: 'invite', inviteId: 'inv1', title: 'Invite to d**@example.com ends today',
        })]);
        expect(first.entries[0].details).toMatch(/^The link stops working at /);
        expect(activityNotifications(activity, first.announced, now).entries).toEqual([]);

        const tomorrow = new Date(2026, 10, 18, 9, 0).getTime() / 1000;
        expect(activityNotifications({ invitesEnding: [{ ...activity.invitesEnding[0], inviteId: 'inv2', expiresAt: tomorrow }] },
            first.announced, now).entries[0].title).toBe('Invite to d**@example.com ends tomorrow');
    });

    it('forgets albums and invites the server no longer lists', () => {
        const { announced } = activityNotifications({}, { opens: { old: 3 }, invites: ['gone'] }, now);
        expect(announced).toEqual({ opens: {}, invites: [] });
    });
});

describe('processing summary', () => {
    it('words it as the redesign does', () => {
        expect(processingSummaryText({ done: true, processedSeconds: 240, failed: 0, people: 9 })).toBe('All processed in 4 minutes. 9 people found.');
        expect(processingSummaryText({ done: true, processedSeconds: 30, failed: 0, people: 1 })).toBe('All processed in under a minute. 1 person found.');
        expect(processingSummaryText({ done: true, processedSeconds: 3900, failed: 0, people: null })).toBe('All processed in 1 hour 5 minutes.');
        expect(processingSummaryText({ done: true, processedSeconds: 120, failed: 2, people: 0 })).toBe('Processed in 2 minutes; 2 couldn’t be processed.');
    });
});
