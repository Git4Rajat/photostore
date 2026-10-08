export type LibraryChangeDomain = 'photos' | 'people' | 'albums' | 'explore' | 'trash';

export type LibraryChange = {
    id: string;
    at: number;
    operation: string;
    domains: LibraryChangeDomain[];
    itemCount?: number;
    jobId?: string;
    entityIds?: string[];
};

const CHANNEL_NAME = 'photostore.library-changes.v1';
const STORAGE_KEY = 'photostore.library-change.latest';
const listeners = new Set<(change: LibraryChange) => void>();
const deliveredIds = new Set<string>();
let channel: BroadcastChannel | null = null;
let installed = false;

const deliver = (change: LibraryChange): void => {
    if (deliveredIds.has(change.id)) return;
    deliveredIds.add(change.id);
    if (deliveredIds.size > 200) {
        deliveredIds.delete(deliveredIds.values().next().value as string);
    }
    listeners.forEach((listener) => {
        try {
            listener(change);
        } catch {
            // One view must not prevent the other views from reconciling.
        }
    });
};

const install = (): void => {
    if (installed || typeof window === 'undefined') return;
    installed = true;
    if (typeof BroadcastChannel !== 'undefined') {
        channel = new BroadcastChannel(CHANNEL_NAME);
        channel.addEventListener('message', (event: MessageEvent<LibraryChange>) => {
            if (event.data?.id) deliver(event.data);
        });
    }
    window.addEventListener('storage', (event) => {
        if (event.key !== STORAGE_KEY || !event.newValue) return;
        try {
            const change = JSON.parse(event.newValue) as LibraryChange;
            if (change?.id) deliver(change);
        } catch {
            // Ignore malformed or older persisted values.
        }
    });
};

export const publishLibraryChange = (
    operation: string,
    domains: LibraryChangeDomain[],
    options: { itemCount?: number; jobId?: string; entityIds?: string[]; notifyCurrentTab?: boolean } = {},
): LibraryChange => {
    install();
    const change: LibraryChange = {
        id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
        at: Date.now(),
        operation,
        domains: Array.from(new Set(domains)),
        itemCount: options.itemCount,
        jobId: options.jobId,
        entityIds: options.entityIds,
    };
    channel?.postMessage(change);
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(change));
    } catch {
        // BroadcastChannel is the primary path; storage is its compatibility fallback.
    }
    if (options.notifyCurrentTab) deliver(change);
    return change;
};

export const subscribeLibraryChanges = (listener: (change: LibraryChange) => void): (() => void) => {
    install();
    listeners.add(listener);
    return () => listeners.delete(listener);
};
