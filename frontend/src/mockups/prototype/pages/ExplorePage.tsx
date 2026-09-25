import React from 'react';
import { useStore } from '../store';

/** Explore — browse by place and by thing, built from data already captured. */
export const ExplorePage: React.FC = () => {
    const { places, things, photos, navigate } = useStore();

    const placeCount = (placeId: string) => photos.filter((p) => p.placeId === placeId).length;

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Explore</h1>
                    <p className="pt-page-sub">Places and things, from your library’s own metadata</p>
                </div>
            </div>

            <div className="pt-bigmap" aria-hidden="true">
                <span className="pt-cluster" style={{ top: '38%', left: '28%' }}>{placeCount('lisbon')}</span>
                <span className="pt-cluster" style={{ top: '60%', left: '58%' }}>{placeCount('goa')}</span>
                <span className="pt-cluster" style={{ top: '26%', left: '72%' }}>{placeCount('tokyo')}</span>
            </div>

            <div className="pt-menu-label">Places</div>
            <div className="pt-place-row">
                {places.map((pl) => (
                    <button key={pl.id} type="button" className={`pt-place-card mock-swatch ${pl.swatch}`} onClick={() => navigate('ask', { query: pl.name })}>
                        <span>{pl.name} · {placeCount(pl.id)}</span>
                    </button>
                ))}
            </div>

            <div className="pt-menu-label" style={{ marginTop: 18 }}>Things</div>
            <div className="pt-things-grid">
                {things.map((t) => (
                    <button key={t.id} type="button" className={`pt-thing-card mock-swatch ${t.swatch}`} onClick={() => navigate('ask', { query: t.name })}>
                        <span>{t.name} · {t.count}</span>
                    </button>
                ))}
            </div>
        </div>
    );
};

export default ExplorePage;
