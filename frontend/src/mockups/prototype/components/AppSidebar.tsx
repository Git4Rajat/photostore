import React, { useEffect } from 'react';
import {
    Heart as HeartIcon,
    Image as PhotoIcon,
    Images as AlbumsIcon,
    MapPin as MapPinIcon,
    Trash2 as TrashIcon,
    Users as UsersIcon,
    UserPlus as LibraryIcon,
    Video as VideoIcon,
} from 'lucide-react';
import { LogoLockup } from '../../../components/shared/Logo';
import { useStore } from '../store';
import { enqueueBackgroundRequest } from '../../../services/backgroundRequestQueue';
import { useProtectedBlobUrls } from '../../../services/imageClient';
import type { PageId } from '../types';

/**
 * The desktop sidebar from the redesign: the places you go, then media types,
 * then your albums, with Recently Deleted and Library & sharing at the foot.
 * Hidden on phones, where the tab bar and the account menu do this job.
 */
export const AppSidebar: React.FC = () => {
    const {
        route, navigate, albums, people, totalPhotos, mediaFilter, setMediaFilter, galleryFilters, setGalleryLikedOnly, fetchAlbums,
    } = useStore();
    // The album list is the sidebar's own content, so load it once the shell is up.
    useEffect(() => {
        const controller = new AbortController();
        void enqueueBackgroundRequest(() => fetchAlbums(), { signal: controller.signal }).catch(() => {});
        return () => controller.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    const covers = useProtectedBlobUrls(albums.map((a) => a.coverThumbnailUrl).filter((u): u is string => Boolean(u)));

    const page = route.page;
    const onGallery = page === 'gallery';
    const plainGallery = onGallery && mediaFilter === 'all' && !galleryFilters.likedOnly;
    const go = (target: PageId) => {
        if (target === 'gallery') { setMediaFilter('all'); setGalleryLikedOnly(false); }
        navigate(target);
    };
    const item = (key: string, label: string, Icon: React.ElementType, on: boolean, onClick: () => void, count?: number | null) => (
        <button key={key} type="button" className={`pt-side-item${on ? ' on' : ''}`} aria-current={on ? 'page' : undefined} onClick={onClick}>
            <Icon aria-hidden="true" />
            <span>{label}</span>
            {count ? <em>{count.toLocaleString()}</em> : null}
        </button>
    );

    return (
        <aside className="pt-side" aria-label="Sections">
            <button type="button" className="pt-side-logo" onClick={() => go('gallery')} aria-label="Go to Photos">
                <LogoLockup size={26} />
            </button>
            {item('photos', 'Photos', PhotoIcon, plainGallery, () => go('gallery'), totalPhotos)}
            {item('albums', 'Albums', AlbumsIcon, page === 'albums', () => navigate('albums'), albums.length)}
            {item('people', 'People', UsersIcon, page === 'people' || page === 'person', () => navigate('people'), people.length)}
            {item('places', 'Places and things', MapPinIcon, page === 'explore', () => navigate('explore'))}

            <div className="pt-side-group">Media</div>
            {item('videos', 'Videos', VideoIcon, onGallery && mediaFilter === 'video', () => { navigate('gallery'); setGalleryLikedOnly(false); setMediaFilter('video'); })}
            {item('liked', 'Liked', HeartIcon, onGallery && galleryFilters.likedOnly, () => { navigate('gallery'); setMediaFilter('all'); setGalleryLikedOnly(true); })}

            {albums.length > 0 && <div className="pt-side-group">Your albums</div>}
            {albums.length > 0 && (
                // Every album, in its own scrolling list (it used to stop at eight with no way to the rest).
                <div className="pt-side-albums">
                    {albums.map((a) => {
                        const on = page === 'albums' && route.params.albumId === a.id;
                        const src = a.coverThumbnailUrl ? covers[a.coverThumbnailUrl] : undefined;
                        return (
                            <button key={a.id} type="button" className={`pt-side-item${on ? ' on' : ''}`} aria-current={on ? 'page' : undefined} onClick={() => navigate('albums', { albumId: a.id })}>
                                {src ? <img className="pt-side-thumb" src={src} alt="" loading="lazy" /> : <span className="pt-side-thumb" aria-hidden="true" />}
                                <span>{a.name}</span>
                                {a.photoCount ? <em>{a.photoCount.toLocaleString()}</em> : null}
                            </button>
                        );
                    })}
                </div>
            )}

            <div className="pt-side-foot">
                {item('deleted', 'Recently deleted', TrashIcon, page === 'trash', () => navigate('trash'))}
                {item('library', 'Library and sharing', LibraryIcon, page === 'sharing', () => navigate('sharing'))}
            </div>
        </aside>
    );
};

export default AppSidebar;
