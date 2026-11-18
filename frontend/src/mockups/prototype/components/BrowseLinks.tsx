import React from 'react';
import { ChevGlyph as ChevronRightIcon, UsersGlyph as UserGroupIcon } from './RedesignIcons';
import { useStore } from '../store';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import { plural } from '../../../utils/format';

/** Albums tab: the way in to People and Places, which have no tab of their own on a phone.
 *  Counts show only once those lists are loaded; the rows never fetch them themselves. */
export const BrowseLinks: React.FC = () => {
    const { navigate, people, peopleTotal, peopleUnnamedTotal, places } = useStore();
    const faces = people.slice(0, 4);
    const covers = useProtectedBlobUrls(faces.map((p) => p.coverThumbnailUrl).filter((u): u is string => Boolean(u)));
    const peopleLine = peopleTotal > 0
        ? `${peopleTotal === 1 ? '1 person' : `${peopleTotal.toLocaleString()} people`}${peopleUnnamedTotal > 0 ? ` · ${peopleUnnamedTotal} to name` : ''}`
        : 'Everyone Keepsake has found';
    const placesLine = places.length > 0 ? plural(places.length, 'place') : 'Where your photos were taken';
    return (
        <nav className="pt-browse-links" aria-label="People and places">
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
                <span className="pt-browse-thumb map" aria-hidden="true" />
                <span><b>Places</b><small>{placesLine}</small></span><ChevronRightIcon aria-hidden="true" className="pt-browse-chev" />
            </button>
        </nav>
    );
};
