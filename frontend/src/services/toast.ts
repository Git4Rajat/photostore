// Lightweight, dependency-free toast that matches the app theme.
// Styling lives in index.css (.app-toast*) so it follows light/dark mode;
// this module only manages the DOM lifecycle. Multiple toasts stack.

let toastStack: HTMLDivElement | null = null;

// Remember the last few messages briefly so a burst of identical failures
// (e.g. rating several photos while the backend is down) collapses into one
// toast instead of a wall of duplicates.
const recentMessages = new Map<string, number>();
const DEDUPE_WINDOW_MS = 4000;

const getStack = (): HTMLDivElement => {
    if (toastStack && document.body.contains(toastStack)) {
        return toastStack;
    }
    const stack = document.createElement('div');
    stack.className = 'app-toast-stack';
    stack.setAttribute('role', 'status');
    stack.setAttribute('aria-live', 'polite');
    document.body.appendChild(stack);
    toastStack = stack;
    return stack;
};

export interface ToastAction {
    label: string;
    onClick: () => void;
}

export interface ToastOptions {
    timeout?: number;
    variant?: 'default' | 'error';
    action?: ToastAction;
    // When set, identical keys within a short window are suppressed. Defaults to
    // the message text.
    dedupeKey?: string;
}

// Accepts either a bare timeout (back-compat) or a full options object.
export const showToast = (message: string, optionsOrTimeout: number | ToastOptions = {}) => {
    const options: ToastOptions = typeof optionsOrTimeout === 'number'
        ? { timeout: optionsOrTimeout }
        : optionsOrTimeout;
    const timeout = options.timeout ?? (options.variant === 'error' ? 6000 : 3000);
    const dedupeKey = options.dedupeKey ?? message;

    const now = Date.now();
    const lastShown = recentMessages.get(dedupeKey);
    if (lastShown !== undefined && now - lastShown < DEDUPE_WINDOW_MS) {
        return;
    }
    recentMessages.set(dedupeKey, now);

    try {
        const el = document.createElement('div');
        el.className = options.variant === 'error' ? 'app-toast app-toast--error' : 'app-toast';

        const text = document.createElement('span');
        text.className = 'app-toast__text';
        text.textContent = message;
        el.appendChild(text);

        let removed = false;
        const remove = () => {
            if (removed) {
                return;
            }
            removed = true;
            el.classList.remove('is-visible');
            const detach = () => {
                try { el.remove(); } catch { /* already gone */ }
            };
            el.addEventListener('transitionend', detach, { once: true });
            // Fallback in case transitionend never fires (reduced motion).
            window.setTimeout(detach, 400);
        };

        if (options.action) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'app-toast__action';
            button.textContent = options.action.label;
            button.addEventListener('click', () => {
                try {
                    options.action?.onClick();
                } finally {
                    remove();
                }
            });
            el.appendChild(button);
        }

        getStack().appendChild(el);
        // Force a layout so the enter transition runs from the initial state.
        void el.offsetHeight;
        el.classList.add('is-visible');
        window.setTimeout(remove, timeout);
    } catch {
        // Toasts are best-effort; never let them break the caller.
    }
};
