// Whether indexer work is worth staying connected for: either something that
// actually touches the index/job state just happened (requestJobPoll() --
// upload-complete, a label/merge/recluster, find-more-faces, etc. -- see
// jobNotifications.ts), or the user is on a tab that itself surfaces live
// index/job state (Tools, Sharing). Outside both, startIndexReadyPush stays
// disconnected so the indexer container can actually scale to zero on pure
// Gallery/Albums/People/Explore/Ask browsing, instead of being kept warm
// forever by a WebSocket no page is using.
export const INDEX_ACTIVITY_WINDOW_MS = 5 * 60 * 1000;

let lastActivity = 0;
let activeTab: 'tools' | 'sharing' | null = null;
const listeners = new Set<() => void>();

export const hasRecentIndexActivity = (): boolean => Date.now() - lastActivity < INDEX_ACTIVITY_WINDOW_MS;
export const isIndexDemandTab = (): boolean => activeTab !== null;
export const hasIndexDemand = (): boolean => hasRecentIndexActivity() || isIndexDemandTab();

export const noteIndexActivity = (): void => {
    const already = hasIndexDemand();
    lastActivity = Date.now();
    if (!already) listeners.forEach(listener => listener());
};

// Called from the router-aware shell on every navigation; `null` when the
// current route isn't one of the two that needs live index/job state.
export const setIndexDemandTab = (tab: 'tools' | 'sharing' | null): void => {
    const already = hasIndexDemand();
    activeTab = tab;
    if (!already && hasIndexDemand()) listeners.forEach(listener => listener());
};

export const onIndexDemandReturn = (listener: () => void): (() => void) => {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
};

// Module-level state persists across tests within the same file even under
// fresh vi.useFakeTimers() calls (each one re-anchors near the real "now",
// not far enough from a previous test's lastActivity to look expired) --
// tests that need a clean slate should call this in beforeEach.
export const __resetIndexDemandForTests = (): void => {
    lastActivity = 0;
    activeTab = null;
};
