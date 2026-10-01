import React, { useEffect } from 'react';
import { useStore } from '../store';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { usePhotoThumbnails } from '../media';
import { Swatch } from '../components/bits';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';

/** A labelled, horizontally-scrolling row of cards (a "shelf"). */
const Shelf: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
    <section className="pt-shelf">
        <h2 className="pt-shelf-title">{title}</h2>
        <div className="pt-shelf-row">{children}</div>
    </section>
);

/**
 * Explore — a stack of horizontally-scrolling shelves (People, Places, Things,
 * Albums, Recently added), each built from data the library already has. Every
 * card deep-links into the relevant view.
 */
export const ExplorePage: React.FC = () => {
    const {
        places, things, people, albums, photos, exploreLoading, peopleLoading, navigate, openViewer,
        fetchExplore, fetchPeople, fetchAlbums,
    } = useStore();

    // Loads everything this page renders when it's actually visited, queued
    // (sequential, not parallel) behind whatever else is in flight, aborted
    // if the user navigates away before its turn. Explore's shelves pull
    // from the SAME people/albums state the People/Albums tabs populate
    // (fetchExplore itself only fills places/things), so a user landing on
    // Explore without having visited those tabs first needs this page to
    // trigger all three itself, or the People/Albums shelves would render
    // empty. photos is covered already -- Gallery's fetch stays boot-time
    // eager. See the 2026-10-01 boot-request audit.
    useEffect(() => {
        const controller = new AbortController();
        void enqueueBackgroundRequest(() => fetchExplore(), { signal: controller.signal }).catch(() => {});
        void enqueueBackgroundRequest(() => fetchPeople(), { signal: controller.signal }).catch(() => {});
        void enqueueBackgroundRequest(() => fetchAlbums(), { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Resolve every group cover in one batched pass (people/place/thing thumbs).
    const covers = useProtectedBlobUrls(
        [...people.map((p) => p.coverThumbnailUrl), ...places.map((p) => p.coverThumbnailUrl), ...things.map((t) => t.coverThumbnailUrl), ...albums.map((a) => a.coverThumbnailUrl)]
            .filter((u): u is string => Boolean(u)),
    );
    const coverFor = (url?: string) => (url ? covers[url] : undefined);

    // A strip of the newest photos, opening straight into the viewer.
    const recent = photos.slice(0, 24);
    const recentThumbs = usePhotoThumbnails(recent);
    const recentIds = recent.map((p) => p.id);

    const namedPeople = people.filter((p) => p.faceCount && p.faceCount > 0);
    const nothingYet = !exploreLoading && !peopleLoading
        && people.length === 0 && places.length === 0 && things.length === 0 && albums.length === 0 && photos.length === 0;

    return (
        <div>
            <div className="pt-toolbar">
                <div>
                    <h1 className="pt-page-title">Explore</h1>
                    <p className="pt-page-sub">Browse your library by people, places, things and moments</p>
                </div>
            </div>

            {(exploreLoading || peopleLoading) && people.length === 0 && places.length === 0 && things.length === 0 && (
                <p className="pt-grid-empty">Gathering people, places and things…</p>
            )}

            {namedPeople.length > 0 && (
                <Shelf title="People">
                    {namedPeople.map((p) => (
                        <button key={p.id} type="button" className="pt-shelf-card person" onClick={() => navigate('person', { personId: p.id })}>
                            {coverFor(p.coverThumbnailUrl)
                                ? <img className="pt-shelf-face" src={coverFor(p.coverThumbnailUrl)} alt={p.name ?? 'Person'} />
                                : <Swatch swatch={p.swatch} className="pt-shelf-face" />}
                            <span className="pt-shelf-card-name">{p.name ?? 'Unnamed'}</span>
                            <span className="pt-shelf-card-sub">{p.faceCount} photos</span>
                        </button>
                    ))}
                </Shelf>
            )}

            {places.length > 0 && (
                <Shelf title="Places">
                    {places.map((pl) => (
                        <button key={pl.id} type="button" className={`pt-shelf-card wide mock-swatch ${pl.swatch}`} onClick={() => navigate('ask', { query: pl.name })}>
                            {coverFor(pl.coverThumbnailUrl) && <img className="pt-explore-cover" src={coverFor(pl.coverThumbnailUrl)} alt={pl.name} />}
                            <span className="pt-shelf-overlay">{pl.name}{pl.count ? ` · ${pl.count}` : ''}</span>
                        </button>
                    ))}
                </Shelf>
            )}

            {things.length > 0 && (
                <Shelf title="Things">
                    {things.map((t) => (
                        <button key={t.id} type="button" className={`pt-shelf-card wide mock-swatch ${t.swatch}`} onClick={() => navigate('ask', { query: t.name })}>
                            {coverFor(t.coverThumbnailUrl) && <img className="pt-explore-cover" src={coverFor(t.coverThumbnailUrl)} alt={t.name} />}
                            <span className="pt-shelf-overlay">{t.name} · {t.count}</span>
                        </button>
                    ))}
                </Shelf>
            )}

            {albums.length > 0 && (
                <Shelf title="Albums">
                    {albums.map((a) => (
                        <button key={a.id} type="button" className="pt-shelf-card wide album" onClick={() => navigate('albums', { albumId: a.id })}>
                            {coverFor(a.coverThumbnailUrl) && <img className="pt-explore-cover" src={coverFor(a.coverThumbnailUrl)} alt={a.name} />}
                            <span className="pt-shelf-overlay">{a.name} · {a.photoCount}</span>
                        </button>
                    ))}
                </Shelf>
            )}

            {recent.length > 0 && (
                <Shelf title="Recently added">
                    {recent.map((p, i) => (
                        <button key={p.id} type="button" className={`pt-shelf-card photo mock-swatch ${p.swatch}`} onClick={() => openViewer(recentIds, i)} aria-label={p.filename}>
                            {recentThumbs[p.filename] && <img className="pt-explore-cover" src={recentThumbs[p.filename]} alt={p.filename} loading="lazy" />}
                        </button>
                    ))}
                </Shelf>
            )}

            {nothingYet && (
                <p className="pt-grid-empty">Once your photos have people, locations and tags, they’ll show up here to explore.</p>
            )}
        </div>
    );
};

export default ExplorePage;
