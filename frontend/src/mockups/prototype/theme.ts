export type Theme = 'light' | 'dark' | 'system';

const THEME_KEY = 'photostore.theme';

/** The theme the user last picked, or 'system' when none is saved or storage is blocked. */
export const loadTheme = (): Theme => {
    try {
        const saved = window.localStorage.getItem(THEME_KEY);
        return saved === 'light' || saved === 'dark' || saved === 'system' ? saved : 'system';
    } catch {
        return 'system';
    }
};

export const saveTheme = (theme: Theme): void => {
    try {
        window.localStorage.setItem(THEME_KEY, theme);
    } catch {
        // Private mode or blocked storage: the choice just won't survive a reload.
    }
};

export const applyTheme = (theme: Theme) => {
    const root = document.documentElement;
    if (theme === 'system') {
        delete root.dataset.theme;
        root.style.colorScheme = '';
    } else {
        root.dataset.theme = theme;
        root.style.colorScheme = theme;
    }
};
