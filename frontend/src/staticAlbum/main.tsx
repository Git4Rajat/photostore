import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import '../index.css';
import '../mockups/mockups.css';
import '../mockups/prototype/prototype.css';
import { wrapExtrasAdapter } from '../services/apiClient';
import PublicAlbumPage from '../components/PublicAlbumPage';
import { snapshotAdapter, type SnapshotManifest } from './snapshotAdapter';

/**
 * The shared-album page, served from the storage static website. It is the same PublicAlbumPage the
 * real app renders; only the album read is answered from the snapshot manifest (see snapshotAdapter).
 * Albums bigger than the cached window hand over to the real app once it has woken up.
 */
interface SnapshotConfig { token: string; spa: string; archive: string; base: string }
declare global { interface Window { __SNAPSHOT__?: SnapshotConfig } }

const cfg = window.__SNAPSHOT__;
const root = document.getElementById('root');

const ping = (url: string): Promise<void> => new Promise((resolve) => {
    let tries = 0;
    const attempt = () => {
        tries += 1;
        fetch(url, { mode: 'no-cors', cache: 'no-store' }).then(() => resolve()).catch(() => {
            if (tries < 8) setTimeout(attempt, 1500); else resolve();
        });
    };
    attempt();
});

if (cfg && root) {
    let manifest: SnapshotManifest | null = null;
    wrapExtrasAdapter(snapshotAdapter(cfg.base, cfg.token, (m) => { manifest = m; }));

    createRoot(root).render(
        <React.StrictMode>
            <MemoryRouter initialEntries={[`/public/album/${encodeURIComponent(cfg.token)}`]}>
                <Routes><Route path="/public/album/:token" element={<PublicAlbumPage />} /></Routes>
            </MemoryRouter>
        </React.StrictMode>,
    );

    // More photos than the snapshot holds: wake the real app in parallel and move over once it answers,
    // but never while the viewer is open.
    const full = `${cfg.spa}/public/album/${encodeURIComponent(cfg.token)}`;
    void Promise.all([ping(`${cfg.spa}/health`), ping(`${cfg.archive}/health`)]).then(() => {
        const timer = window.setInterval(() => {
            const m = manifest as SnapshotManifest | null;
            if (!m) return;
            if (m.total <= m.cached) { window.clearInterval(timer); return; }
            if (document.querySelector('[role="dialog"]')) return;
            window.clearInterval(timer);
            window.location.replace(full);
        }, 500);
    });
}
