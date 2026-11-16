import React from 'react';
import { ChevronRight as ChevronRightIcon, MapPin as MapPinIcon, Users as UserGroupIcon, Video as VideoIcon } from 'lucide-react';
import { useStore } from '../store';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { plural } from '../../../utils/format';

/** Albums tab: the way in to People, Places and media types, which have no tab of their own on a phone.
 *  Counts show only once those lists are loaded; the rows never fetch them themselves. */
export const BrowseLinks: React.FC = () => {
    const { navigate, people, peopleTotal, peopleUnnamedTotal, places, setMediaFilter, setGalleryLikedOnly } = useStore();
    const faces = people.slice(0, 4);
    const covers = useProtectedBlobUrls(faces.map((p) => p.coverThumbnailUrl).filter((u): u is string => Boolean(u)));
    const peopleLine = peopleTotal > 0
        ? `${peopleTotal === 1 ? '1 person' : `${peopleTotal.toLocaleString()} people`}${peopleUnnamedTotal > 0 ? ` · ${peopleUnnamedTotal} to name` : ''}`
        : 'Everyone Keepsake has found';
    const placesLine = places.length > 0 ? plural(places.length, 'place') : 'Browse by where and what';
    return (
        <>
            <nav className="pt-browse-links" aria-label="Browse by">
                <button type="button" onClick={() => navigate('people')}>
                    {faces.some((p) => p.coverThumbnailUrl && covers[p.coverThumbnailUrl]) ? (
                        <span className="pt-browse-thumb mosaic" aria-hidden="true">
                            {faces.map((p) => (p.coverThumbnailUrl && covers[p.coverThumbnailUrl]
                                ? <img key={p.id} src={covers[p.coverThumbnailUrl]} alt="" />
                                : <i key={p.id} />))}
                        </span>
                    ) : <span className="pt-browse-thumb" aria-hidden="true"><UserGroupIcon /></span>}
                    <span><b>People</b><small>{peopleLine}</small></span><ChevronRightIcon aria-hidden="true" className="pt-browse-chev" />
                </button>
                <button type="button" onClick={() => navigate('explore')}>
                    <span className="pt-browse-thumb map" aria-hidden="true"><MapPinIcon /></span>
                    <span><b>Places and things</b><small>{placesLine}</small></span><ChevronRightIcon aria-hidden="true" className="pt-browse-chev" />
                </button>
            </nav>
            <h2 className="pt-section-label">Media types</h2>
            <nav className="pt-browse-links" aria-label="Media types">
                <button type="button" onClick={() => { navigate('gallery'); setGalleryLikedOnly(false); setMediaFilter('video'); }}>
                    <span className="pt-browse-thumb plain" aria-hidden="true"><VideoIcon /></span>
                    <span><b>Videos</b></span><ChevronRightIcon aria-hidden="true" className="pt-browse-chev" />
                </button>
            </nav>
        </>
    );
};
