export const showToast = (message: string, timeout = 3000) => {
    try {
        const id = `toast-${Date.now()}`;
        const el = document.createElement('div');
        el.id = id;
        el.textContent = message;
        el.style.position = 'fixed';
        el.style.top = '16px';
        el.style.right = '16px';
        el.style.background = 'rgba(0,0,0,0.85)';
        el.style.color = '#fff';
        el.style.padding = '8px 12px';
        el.style.borderRadius = '6px';
        el.style.zIndex = '9999';
        el.style.boxShadow = '0 4px 12px rgba(0,0,0,0.3)';
        el.style.fontSize = '14px';
        document.body.appendChild(el);
        setTimeout(() => {
            try { el.style.transition = 'opacity 0.3s'; el.style.opacity = '0'; } catch (e) {}
            setTimeout(() => { try { el.remove(); } catch (e) {} }, 300);
        }, timeout);
    } catch (e) {
        // ignore
    }
};
