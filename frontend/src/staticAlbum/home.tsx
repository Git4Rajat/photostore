import React from 'react';
import { createRoot } from 'react-dom/client';
import { HomeChrome, SkeletonTiles } from '../mockups/prototype/components/HomeChrome';

/**
 * The static front door's real-UI screen: the app's own header and gallery frame with placeholder tiles,
 * shown from the storage static website while the web app wakes. The page's inline script does the waking
 * and the hand-over; this only draws.
 */
export const mountHome = (root: HTMLElement) => {
    createRoot(root).render(
        <React.StrictMode>
            <HomeChrome subtitle="Opening your library…"><SkeletonTiles /></HomeChrome>
        </React.StrictMode>,
    );
};
