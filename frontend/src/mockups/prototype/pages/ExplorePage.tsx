import React from 'react';
import { useStore } from '../store';
import { useProtectedBlobUrls } from '../../../services/imageClient';

/** Explore — browse by place and by thing, built from data already captured. */
export const ExplorePage: React.FC = () => {
    const { places, things, exploreLoading, navigate } = useStore();
    const covers = useProtectedBlobUrls(
        [...places, ...things].map((g) => g.coverThumbnailUrl).filter((u): u is string => Boolean(u)),
    );

    const coverFor = (url?: string) => (url ? covers[url] : undefined);

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Explore</h1>
                    <p className="pt-page-sub">Places and things, from your library’s own metadata</p>
                </div>
            </div>

            {exploreLoading && places.length === 0 && things.length === 0 && (
                <p className="pt-grid-empty">Finding places and things…</p>
            )}

            {places.length > 0 && (
                <>
                    <div className="pt-menu-label">Places</div>
                    <div className="pt-place-row">
                        {places.map((pl) => {
                            const src = coverFor(pl.coverThumbnailUrl);
                            return (
                                <button key={pl.id} type="button" className={`pt-place-card mock-swatch ${pl.swatch}`} onClick={() => navigate('ask', { query: pl.name })}>
                                    {src && <img className="pt-explore-cover" src={src} alt={pl.name} />}
                                    <span>{pl.name} · {pl.count ?? 0}</span>
                                </button>
                            );
                        })}
                    </div>
                </>
            )}

            {things.length > 0 && (
                <>
                    <div className="pt-menu-label" style={{ marginTop: 18 }}>Things</div>
                    <div className="pt-things-grid">
                        {things.map((t) => {
                            const src = coverFor(t.coverThumbnailUrl);
                            return (
                                <button key={t.id} type="button" className={`pt-thing-card mock-swatch ${t.swatch}`} onClick={() => navigate('ask', { query: t.name })}>
                                    {src && <img className="pt-explore-cover" src={src} alt={t.name} />}
                                    <span>{t.name} · {t.count}</span>
                                </button>
                            );
                        })}
                    </div>
                </>
            )}

            {!exploreLoading && places.length === 0 && things.length === 0 && (
                <p className="pt-grid-empty">Once your photos have locations and tags, they’ll show up here grouped by place and subject.</p>
            )}
        </div>
    );
};

export default ExplorePage;
