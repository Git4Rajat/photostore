import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MapPinIcon } from '@heroicons/react/24/outline';
import { get } from '../services/apiClient';
import PhotoTile from './shared/PhotoTile';
import { EmptyState } from './shared/EmptyState';
import { Loading } from './shared/Loading';
import { ErrorState } from './shared/ErrorState';
import { classifyApiError, type ApiError } from '../services/apiError';
import { useBackendRecoveryRetry } from '../services/useBackendRecoveryRetry';
import { useThumbnailAccessResolver } from '../services/useThumbnailAccessResolver';
import { plural } from '../utils/format';
import type { Photo } from '../types/uiTypes';

interface ExploreGroup {
    label: string;
    count: number;
    photo: Photo;
    latitude?: string;
    longitude?: string;
}

const ExplorePage: React.FC = () => {
    const navigate = useNavigate();
    const [places, setPlaces] = useState<ExploreGroup[]>([]);
    const [things, setThings] = useState<ExploreGroup[]>([]);
    const [loading, setLoading] = useState<boolean>(true);
    const [error, setError] = useState<ApiError | null>(null);
    const { thumbAccessUrls, resolveAccessForBatch } = useThumbnailAccessResolver();

    const load = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const data = await get('/explore');
            const nextPlaces: ExploreGroup[] = Array.isArray(data?.places) ? data.places : [];
            const nextThings: ExploreGroup[] = Array.isArray(data?.things) ? data.things : [];
            setPlaces(nextPlaces);
            setThings(nextThings);
            resolveAccessForBatch([...nextPlaces, ...nextThings].map((group) => group.photo));
        } catch (err) {
            setError(classifyApiError(err));
        } finally {
            setLoading(false);
        }
    }, [resolveAccessForBatch]);

    useEffect(() => {
        void load();
    }, [load]);

    useBackendRecoveryRetry(error, load);

    // Reuses the existing search index (already matches location + tag/object
    // text) rather than a bespoke filter endpoint -- a place/thing label is
    // just a search query.
    const goToSearch = (label: string) => {
        navigate(`/?q=${encodeURIComponent(label)}`);
    };

    // Overlay-card style (bg photo + bottom scrim + label/count baked over the
    // image) for both Places and Things -- reuses PhotoTile's existing
    // access-token/batched-thumbnail-resolution logic wholesale via its
    // mediaOverlay slot rather than re-implementing image loading here.
    const renderGroup = (group: ExploreGroup, variant: 'place' | 'thing') => (
        <PhotoTile
            key={group.label}
            photo={group.photo}
            title={group.label}
            useBatchedAccess
            resolvedAccessUrl={thumbAccessUrls.get(group.photo.filename)}
            onCardClick={() => goToSearch(group.label)}
            showBody={false}
            className={`explore-card explore-card-${variant}`}
            mediaOverlay={(
                <span className="explore-card-scrim">
                    <span className="explore-card-label">{group.label}</span>
                    <span className="explore-card-count">{plural(group.count, 'photo')}</span>
                </span>
            )}
        />
    );

    // Decorative (non-interactive, no map vendor) scatter of Places by their
    // already-captured lat/long -- min/max-normalized across this user's own
    // places into a bounded box, same spirit as the Ask search's decorative
    // map-snippet. Net new backend work: zero, latitude/longitude were
    // already returned by GET /explore and simply unused until now.
    const geoPlaces = places
        .map((place) => ({ place, lat: parseFloat(place.latitude || ''), lon: parseFloat(place.longitude || '') }))
        .filter((entry) => Number.isFinite(entry.lat) && Number.isFinite(entry.lon));
    const lats = geoPlaces.map((entry) => entry.lat);
    const lons = geoPlaces.map((entry) => entry.lon);
    const minLat = lats.length ? Math.min(...lats) : 0;
    const maxLat = lats.length ? Math.max(...lats) : 0;
    const minLon = lons.length ? Math.min(...lons) : 0;
    const maxLon = lons.length ? Math.max(...lons) : 0;
    const latRange = maxLat - minLat;
    const lonRange = maxLon - minLon;

    const isEmpty = !loading && !error && places.length === 0 && things.length === 0;

    return (
        <section className="card-glass gallery-wrap">
            <header className="page-topline">
                <h2 className="page-topline-title">Explore</h2>
                <p className="gallery-meta-line">
                    <span className="gallery-meta-dim">Browse your library by place and subject</span>
                </p>
            </header>

            {loading && <Loading label="Finding places and things…" fullPage={false} />}
            {error && (
                <ErrorState
                    title="Couldn't load Explore"
                    message={error.message}
                    onRetry={error.retriable ? () => { void load(); } : undefined}
                />
            )}
            {isEmpty && (
                <EmptyState
                    icon={<MapPinIcon />}
                    title="Nothing to explore yet"
                    message="Once your photos have locations and tags, they'll show up here grouped by place and subject."
                />
            )}

            {!loading && !error && geoPlaces.length > 0 && (
                <div className="explore-map" aria-hidden="true">
                    {geoPlaces.map(({ place, lat, lon }) => (
                        <span
                            key={place.label}
                            className="explore-map-pin"
                            style={{
                                left: `${lonRange ? ((lon - minLon) / lonRange) * 80 + 10 : 50}%`,
                                top: `${latRange ? (1 - (lat - minLat) / latRange) * 80 + 10 : 50}%`,
                            }}
                            title={`${place.label} · ${place.count}`}
                        >
                            {place.count}
                        </span>
                    ))}
                </div>
            )}

            {!loading && !error && places.length > 0 && (
                <>
                    <h3 className="explore-section-title">Places</h3>
                    <div className="explore-row">
                        {places.map((group) => renderGroup(group, 'place'))}
                    </div>
                </>
            )}

            {!loading && !error && things.length > 0 && (
                <>
                    <h3 className="explore-section-title">Things</h3>
                    <div className="gallery-grid">
                        {things.map((group) => renderGroup(group, 'thing'))}
                    </div>
                </>
            )}
        </section>
    );
};

export default ExplorePage;
