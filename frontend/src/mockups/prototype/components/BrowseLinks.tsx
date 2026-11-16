import React from 'react';
import { ChevronRight as ChevronRightIcon, MapPin as MapPinIcon, Users as UserGroupIcon } from 'lucide-react';
import { useStore } from '../store';

/** Albums tab: the way in to People and Explore, which have no tab of their own on a phone. */
export const BrowseLinks: React.FC = () => {
    const { navigate } = useStore();
    return (
        <nav className="pt-browse-links" aria-label="Browse by">
            <button type="button" onClick={() => navigate('people')}>
                <UserGroupIcon aria-hidden="true" /><span><b>People</b><small>Everyone Keepsake has found</small></span><ChevronRightIcon aria-hidden="true" className="pt-browse-chev" />
            </button>
            <button type="button" onClick={() => navigate('explore')}>
                <MapPinIcon aria-hidden="true" /><span><b>Places and things</b><small>Browse by where and what</small></span><ChevronRightIcon aria-hidden="true" className="pt-browse-chev" />
            </button>
        </nav>
    );
};
