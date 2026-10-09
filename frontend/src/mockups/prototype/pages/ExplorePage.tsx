import React, { useEffect, useRef, useState } from 'react';
import { useStore } from '../store';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { usePhotoThumbnails } from '../media';
import { TileThumbnail } from '../components/PhotoGrid';
import { Swatch } from '../components/bits';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';

const SHELF_PAGE = 16;

/** A labelled, horizontally-scrolling row of cards. It renders (and resolves covers for) only the
 *  first few cards and adds more as the row is scrolled toward its end; when every loaded card is
 *  showing, ``onNearEnd`` lets the owner fetch the next server page. */
function Shelf<T>({ title, items, cover, render, onNearEnd }: {
    title: string;
    items: T[];
    cover: (item: T) => string | undefined;
    render: (item: T, coverSrc: string | undefined) => React.ReactNode;
    onNearEnd?: () => void;
}) {
    const [visible, setVisible] = useState(SHELF_PAGE);
    const rowRef = useRef<HTMLDivElement>(null);
    const endRef = useRef<HTMLDivElement>(null);
    const shown = items.slice(0, visible);
    const covers = useProtectedBlobUrls(shown.map(cover).filter((u): u is string => Boolean(u)));

    useEffect(() => {
        const node = endRef.current;
        if (!node) return undefined;
        const observer = new IntersectionObserver((entries) => {
            if (!entries.some((e) => e.isIntersecting)) return;
            if (visible < items.length) setVisible((v) => v + SHELF_PAGE);
            else onNearEnd?.();
        }, { root: rowRef.current, rootMargin: '0px 300px 0px 0px' });
        observer.observe(node);
        return () => observer.disconnect();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [visible, items.length, onNearEnd]);

    return (
        <section className="pt-shelf">
            <h2 className="pt-shelf-title">{title}</h2>
            <div className="pt-shelf-row" ref={rowRef}>
                {shown.map((item) => render(item, (() => { const c = cover(item); return c ? covers[c] : undefined; })()))}
                <div ref={endRef} className="pt-shelf-end" aria-hidden="true" />
            </div>
        </section>
    );
}

/** A fixed shelf (no paging): the strip of recent photos. */
const PlainShelf: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
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
        places, things, people, peopleHasMore, loadMorePeople, albums, photos, exploreLoading, peopleLoading, navigate, openViewer,
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
                <Shelf
                    title="People"
                    items={namedPeople}
                    cover={(p) => p.coverThumbnailUrl}
                    onNearEnd={peopleHasMore ? loadMorePeople : undefined}
                    render={(p, src) => (
                        <button key={p.id} type="button" className="pt-shelf-card person" onClick={() => navigate('person', { personId: p.id })}>
                            {src
                                ? <img className="pt-shelf-face" src={src} alt={p.name ?? 'Person'} />
                                : <Swatch swatch={p.swatch} className="pt-shelf-face" />}
                            <span className="pt-shelf-card-name">{p.name ?? 'Unnamed'}</span>
                            <span className="pt-shelf-card-sub">{p.faceCount} photos</span>
                        </button>
                    )}
                />
            )}

            {places.length > 0 && (
                <Shelf
                    title="Places"
                    items={places}
                    cover={(pl) => pl.coverThumbnailUrl}
                    render={(pl, src) => (
                        <button key={pl.id} type="button" className={`pt-shelf-card wide mock-swatch ${pl.swatch}`} onClick={() => navigate('ask', { query: pl.name })}>
                            {src && <TileThumbnail key={src} src={src} alt={pl.name} className="pt-explore-cover" />}
                            <span className="pt-shelf-overlay">{pl.name}{pl.count ? ` · ${pl.count}` : ''}</span>
                        </button>
                    )}
                />
            )}

            {things.length > 0 && (
                <Shelf
                    title="Things"
                    items={things}
                    cover={(t) => t.coverThumbnailUrl}
                    render={(t, src) => (
                        <button key={t.id} type="button" className={`pt-shelf-card wide mock-swatch ${t.swatch}`} onClick={() => navigate('ask', { query: t.name })}>
                            {src && <TileThumbnail key={src} src={src} alt={t.name} className="pt-explore-cover" />}
                            <span className="pt-shelf-overlay">{t.name} · {t.count}</span>
                        </button>
                    )}
                />
            )}

            {albums.length > 0 && (
                <Shelf
                    title="Albums"
                    items={albums}
                    cover={(a) => a.coverThumbnailUrl}
                    render={(a, src) => (
                        <button key={a.id} type="button" className="pt-shelf-card wide album" onClick={() => navigate('albums', { albumId: a.id })}>
                            {src && <TileThumbnail key={src} src={src} alt={a.name} className="pt-explore-cover" />}
                            <span className="pt-shelf-overlay">{a.name} · {a.photoCount}</span>
                        </button>
                    )}
                />
            )}

            {recent.length > 0 && (
                <PlainShelf title="Recently added">
                    {recent.map((p, i) => (
                        <button key={p.id} type="button" className={`pt-shelf-card photo mock-swatch ${p.swatch}`} onClick={() => openViewer(recentIds, i)} aria-label={p.filename}>
                            {recentThumbs[p.filename] && (
                                <TileThumbnail key={recentThumbs[p.filename]} src={recentThumbs[p.filename]} alt={p.filename} className="pt-explore-cover" />
                            )}
                        </button>
                    ))}
                </PlainShelf>
            )}

            {nothingYet && (
                <p className="pt-grid-empty">Once your photos have people, locations and tags, they’ll show up here to explore.</p>
            )}
        </div>
    );
};

export default ExplorePage;
