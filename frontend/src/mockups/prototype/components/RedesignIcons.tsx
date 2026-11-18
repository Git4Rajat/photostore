// The redesign's own glyph set (docs/design-mockups: 24px grid, 1.75 round strokes), for the action bars
// and anywhere else the stock lucide shapes read differently from the mock-up.
import React from 'react';

type IconProps = React.SVGProps<SVGSVGElement> & { fill?: string };

const make = (children: React.ReactNode): React.FC<IconProps> => {
    const Icon: React.FC<IconProps> = ({ fill, ...props }) => (
        <svg viewBox="0 0 24 24" fill={fill ?? 'none'} stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
            {children}
        </svg>
    );
    return Icon;
};

export const HeartGlyph = make(<path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6C19.5 15.4 12 20 12 20z" />);
export const AddToAlbumGlyph = make(<><rect x="3.5" y="7.5" width="13" height="13" rx="2.5" /><path d="M7.5 4.5h10a3 3 0 0 1 3 3v10" /><path d="M10 11v6M7 14h6" /></>);
export const RemoveFromAlbumGlyph = make(<><rect x="3.5" y="7.5" width="13" height="13" rx="2.5" /><path d="M7.5 4.5h10a3 3 0 0 1 3 3v10" /><path d="M7 14h6" /></>);
export const TrashGlyph = make(<><path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l.9 12.2a1.5 1.5 0 0 0 1.5 1.3h6.2a1.5 1.5 0 0 0 1.5-1.3L17.5 7" /><path d="M10.2 11v5.5M13.8 11v5.5" /></>);
export const MoreGlyph = make(<>
    <circle cx="5.5" cy="12" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    <circle cx="18.5" cy="12" r="1.4" fill="currentColor" stroke="none" />
</>);
export const InfoGlyph = make(<><circle cx="12" cy="12" r="8.5" /><path d="M12 11v5.5" /><circle cx="12" cy="7.8" r="1" fill="currentColor" stroke="none" /></>);
export const StarGlyph = make(<path d="M12 4l2.4 5 5.4.6-4 3.7 1.1 5.4L12 16l-4.9 2.7 1.1-5.4-4-3.7 5.4-.6z" />);
export const DownloadGlyph = make(<><path d="M12 4v11M7.5 10.5L12 15l4.5-4.5" /><path d="M5 19.5h14" /></>);
export const SlidersGlyph = make(<><path d="M4 7.5h9M17 7.5h3M4 16.5h3M11 16.5h9" /><circle cx="15" cy="7.5" r="2" /><circle cx="9" cy="16.5" r="2" /></>);
export const SearchGlyph = make(<><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4.5 4.5" /></>);
export const PinGlyph = make(<><path d="M12 20.5s-6.5-5.7-6.5-10.7a6.5 6.5 0 0 1 13 0c0 5-6.5 10.7-6.5 10.7z" /><circle cx="12" cy="9.8" r="2.3" /></>);
