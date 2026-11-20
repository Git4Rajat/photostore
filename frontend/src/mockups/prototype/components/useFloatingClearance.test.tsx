import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { FLOAT_CLEAR_VAR, useFloatingClearance } from './useFloatingClearance';

const Probe: React.FC = () => {
    useFloatingClearance();
    return null;
};

const addDock = (className: string, rect: Partial<DOMRect>) => {
    const el = document.createElement('div');
    el.className = className;
    el.getBoundingClientRect = () => ({ top: 0, left: 0, right: 0, width: 0, height: 0, ...rect } as DOMRect);
    document.body.appendChild(el);
    return el;
};

const flush = async () => {
    await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 30));
    });
};
const clearance = () => document.documentElement.style.getPropertyValue(FLOAT_CLEAR_VAR);

describe('useFloatingClearance', () => {
    beforeEach(() => {
        vi.stubGlobal('innerWidth', 1280);
        vi.stubGlobal('innerHeight', 800);
    });
    afterEach(() => {
        document.body.innerHTML = '';
        vi.unstubAllGlobals();
    });

    it('sets nothing while no dock is showing', async () => {
        render(<Probe />);
        await flush();
        expect(clearance()).toBe('');
    });

    it('rises above a bottom-right dock and drops back when it goes', async () => {
        render(<Probe />);
        const tray = addDock('up-dock up-tray', { top: 600, left: 888, right: 1260, width: 372, height: 180 });
        await flush();
        expect(clearance()).toBe('212px');
        tray.remove();
        await flush();
        expect(clearance()).toBe('');
    });

    it('ignores surfaces that are clear of the button strip', async () => {
        render(<Probe />);
        addDock('pt-action-bar', { top: 700, left: 400, right: 800, width: 400, height: 60 });
        await flush();
        expect(clearance()).toBe('');
    });

    it('uses the tallest overlapping surface', async () => {
        render(<Probe />);
        addDock('up-dock up-bar', { top: 700, left: 12, right: 1268, width: 1256, height: 60 });
        addDock('pt-toast', { top: 650, left: 1000, right: 1200, width: 200, height: 40 });
        await flush();
        expect(clearance()).toBe('162px');
    });
});
