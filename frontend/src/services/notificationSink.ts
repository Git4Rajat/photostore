// Bridge between popup-style feedback (showToast, the prototype store's toast)
// and the notification panel owned by AppServicesProvider. The provider
// registers its addNotification here; anything that used to be popup-only
// publishes through publishNotification so it also lands in the panel (and
// bumps the unread badge).

export type NotificationSink = (title: string, details: string) => void;

let sink: NotificationSink | null = null;

export const setNotificationSink = (next: NotificationSink | null): (() => void) => {
    sink = next;
    return () => {
        if (sink === next) {
            sink = null;
        }
    };
};

export const hasNotificationSink = (): boolean => sink !== null;

// Returns true when a panel accepted the notification.
export const publishNotification = (title: string, details = ''): boolean => {
    if (!sink) {
        return false;
    }
    try {
        sink(title, details);
        return true;
    } catch {
        return false;
    }
};
