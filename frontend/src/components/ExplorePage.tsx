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

    const renderGroup = (group: ExploreGroup) => (
        <PhotoTile
            key={group.label}
            photo={group.photo}
            title={group.label}
            useBatchedAccess
            resolvedAccessUrl={thumbAccessUrls.get(group.photo.filename)}
            onCardClick={() => goToSearch(group.label)}
            bodyContent={(
                <>
                    <p className="photo-kind">{group.label}</p>
                    <p className="gallery-meta-dim">{plural(group.count, 'photo')}</p>
                </>
            )}
        />
    );

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

            {!loading && !error && places.length > 0 && (
                <>
                    <h3 className="explore-section-title">Places</h3>
                    <div className="gallery-grid">
                        {places.map(renderGroup)}
                    </div>
                </>
            )}

            {!loading && !error && things.length > 0 && (
                <>
                    <h3 className="explore-section-title">Things</h3>
                    <div className="gallery-grid">
                        {things.map(renderGroup)}
                    </div>
                </>
            )}
        </section>
    );
};

export default ExplorePage;
