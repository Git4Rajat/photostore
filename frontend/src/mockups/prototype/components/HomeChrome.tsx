import React from 'react';
import { Plus as PlusIcon } from 'lucide-react';
import { LogoLockup } from '../../../components/shared/Logo';

/** Primary navigation, shared by the live header and the read-only home previews. */
export const NAV_ITEMS = [
    { id: 'ask', label: 'Ask' },
    { id: 'gallery', label: 'Gallery' },
    { id: 'albums', label: 'Albums' },
    { id: 'people', label: 'People' },
    { id: 'explore', label: 'Explore' },
    { id: 'tools', label: 'Tools' },
    { id: 'sharing', label: 'Sharing' },
] as const;

/**
 * The app's real header and page frame with inert controls: used while the library is still loading
 * (device-cached preview, and the static front door) so those screens are the same markup and CSS as the app.
 */
export const HomeChrome: React.FC<{ subtitle: string; tileMin?: number; children: React.ReactNode }> = ({ subtitle, tileMin, children }) => (
    <div className="pt-shell">
        <div className="mock-stage">
            <div className="mock-viewport">
                <div className="mock-app">
                    <header className="ios-header">
                        <span className="pt-wordmark-btn"><LogoLockup size={30} className="ios-header-wordmark" /></span>
                        <nav className="ios-tabs" aria-label="Primary navigation">
                            {NAV_ITEMS.map((item) => (
                                <span key={item.id} className={`ios-tab${item.id === 'gallery' ? ' active' : ''}`}>{item.label}</span>
                            ))}
                        </nav>
                        <div className="app-header-actions" aria-hidden="true">
                            <span className="btn mock-cta pt-upload-btn"><PlusIcon className="toolbar-icon" /> <span className="pt-upload-label">Upload</span></span>
                            <span className="mock-avatar pt-avatar-btn" />
                        </div>
                    </header>
                    <div className="mock-body pt-body" aria-busy="true">
                        <div className="pt-toolbar">
                            <div>
                                <h1 className="pt-page-title">Gallery</h1>
                                <p className="pt-page-sub" role="status">{subtitle}</p>
                            </div>
                        </div>
                        <div className="pt-grid" style={tileMin ? ({ ['--pt-tile-min' as string]: `${tileMin}px` } as React.CSSProperties) : undefined}>
                            {children}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
);

export const SkeletonTiles: React.FC<{ count?: number }> = ({ count = 60 }) => (
    <>{Array.from({ length: count }, (_, i) => <div key={i} className="pt-tile pt-tile-skeleton" aria-hidden="true" />)}</>
);
