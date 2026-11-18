// The redesign's own glyph set (docs/design-mockups: 24px grid, 1.75 round strokes), for the action bars
// and anywhere else the stock lucide shapes read differently from the mock-up.
import React from 'react';

type IconProps = React.SVGProps<SVGSVGElement> & { fill?: string; size?: number | string };

const make = (children: React.ReactNode): React.FC<IconProps> => {
    const Icon: React.FC<IconProps> = ({ fill, size = 24, ...props }) => (
        <svg viewBox="0 0 24 24" width={size} height={size} fill={fill ?? 'none'} stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
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
export const PhotosGlyph = make(<><rect x="3.5" y="3.5" width="7" height="7" rx="1.6" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.6" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.6" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.6" /></>);
export const AlbumsGlyph = make(<><rect x="3.5" y="7.5" width="17" height="13" rx="2.5" /><path d="M6.5 4.5h11" /></>);
export const UploadGlyph = make(<><path d="M7.5 18.5H7a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 9.5a4 4 0 0 1-.5 9H17" /><path d="M12 12.5v8M9 15.5l3-3 3 3" /></>);
export const BellGlyph = make(<><path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15z" /><path d="M10 20.5a2.2 2.2 0 0 0 4 0" /></>);
export const ShareGlyph = make(<><path d="M12 3.5v11M8 7.5l4-4 4 4" /><path d="M8 11H6.5A1.5 1.5 0 0 0 5 12.5v6A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5v-6A1.5 1.5 0 0 0 17.5 11H16" /></>);
export const CheckGlyph = make(<path d="M5.5 12.5l4.2 4.2L18.5 8" />);
export const CloseGlyph = make(<path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />);
export const BackGlyph = make(<path d="M14.5 5.5L8 12l6.5 6.5" />);
export const ChevGlyph = make(<path d="M9.5 5.5L16 12l-6.5 6.5" />);
export const LiveGlyph = make(<><circle cx="12" cy="12" r="2.6" /><circle cx="12" cy="12" r="5.6" /><circle cx="12" cy="12" r="8.6" strokeDasharray="1.6 2.4" /></>);
export const LockGlyph = make(<><rect x="5" y="10.5" width="14" height="10" rx="2.5" /><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5" /></>);
export const LinkGlyph = make(<><path d="M10.5 13.5a3.8 3.8 0 0 0 5.4 0l2.7-2.7a3.8 3.8 0 0 0-5.4-5.4l-1 1" /><path d="M13.5 10.5a3.8 3.8 0 0 0-5.4 0l-2.7 2.7a3.8 3.8 0 0 0 5.4 5.4l1-1" /></>);
export const UsersGlyph = make(<><circle cx="9" cy="8.5" r="3.3" /><path d="M3 19.5a6 6 0 0 1 12 0" /><path d="M15.5 5.4a3.3 3.3 0 0 1 0 6.2M17.5 14a6 6 0 0 1 3.5 5.5" /></>);
export const UserPlusGlyph = make(<><circle cx="10" cy="8.5" r="3.3" /><path d="M4 19.5a6 6 0 0 1 12 0" /><path d="M19 8v6M16 11h6" /></>);
export const MailGlyph = make(<><rect x="3.5" y="5.5" width="17" height="13" rx="2.5" /><path d="M4.5 7l7.5 6 7.5-6" /></>);
export const ClockGlyph = make(<><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>);
export const AlertGlyph = make(<><path d="M12 4.5l8.5 15h-17z" /><path d="M12 10v4" /><circle cx="12" cy="16.8" r="1" fill="currentColor" stroke="none" /></>);
export const RefreshGlyph = make(<><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3" /><path d="M19.5 4.5v4.8h-4.8" /></>);
export const CopyGlyph = make(<><rect x="8.5" y="8.5" width="11" height="11" rx="2.2" /><path d="M15.5 8.5V6.7a2.2 2.2 0 0 0-2.2-2.2H6.7a2.2 2.2 0 0 0-2.2 2.2v6.6a2.2 2.2 0 0 0 2.2 2.2h1.8" /></>);
export const PencilGlyph = make(<path d="M5 19l1-4L15.5 5.5a2.1 2.1 0 0 1 3 3L9 18z" />);
export const CalendarGlyph = make(<><rect x="3.5" y="5" width="17" height="15.5" rx="2.5" /><path d="M3.5 9.5h17M8 3v4M16 3v4" /></>);
export const VideoGlyph = make(<><rect x="3.5" y="6.5" width="12" height="11" rx="2.5" /><path d="M15.5 10.5l5-3v9l-5-3" /></>);
export const PlusGlyph = make(<path d="M12 5v14M5 12h14" />);
export const SunGlyph = make(<><circle cx="12" cy="12" r="4" /><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4" /></>);
export const MoonGlyph = make(<path d="M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10z" />);
export const EyeGlyph = make(<><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" /><circle cx="12" cy="12" r="2.8" /></>);
