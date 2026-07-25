// Lightweight, dependency-free toast that matches the app theme.
// Styling lives in index.css (.app-toast*) so it follows light/dark mode;
// this module only manages the DOM lifecycle. Multiple toasts stack.

let toastStack: HTMLDivElement | null = null;

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

export const showToast = (message: string, timeout = 3000) => {
    try {
        const el = document.createElement('div');
        el.className = 'app-toast';
        el.textContent = message;
        getStack().appendChild(el);
        // Force a layout so the enter transition runs from the initial state.
        void el.offsetHeight;
        el.classList.add('is-visible');
        window.setTimeout(() => {
            el.classList.remove('is-visible');
            const remove = () => {
                try { el.remove(); } catch { /* already gone */ }
            };
            el.addEventListener('transitionend', remove, { once: true });
            // Fallback in case transitionend never fires (reduced motion).
            window.setTimeout(remove, 400);
        }, timeout);
    } catch {
        // Toasts are best-effort; never let them break the caller.
    }
};
