import { afterEach, describe, expect, it, vi } from 'vitest';
import { loadTheme, saveTheme } from './theme';

describe('theme preference', () => {
    afterEach(() => {
        vi.restoreAllMocks();
        try { window.localStorage.clear(); } catch { /* storage-less run */ }
    });

    it('survives a reload', () => {
        expect(loadTheme()).toBe('system');
        saveTheme('dark');
        expect(loadTheme()).toBe('dark');
        saveTheme('system');
        expect(loadTheme()).toBe('system');
    });

    it('ignores a value it did not write', () => {
        window.localStorage.setItem('photostore.theme', 'sepia');
        expect(loadTheme()).toBe('system');
    });

    it('falls back to system when storage is blocked', () => {
        vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('SecurityError'); });
        vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('SecurityError'); });
        expect(() => saveTheme('light')).not.toThrow();
        expect(loadTheme()).toBe('system');
    });
});
