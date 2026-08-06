import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { PhotoIcon, UsersIcon, WrenchScrewdriverIcon } from '@heroicons/react/24/outline';
import type { PhotoPersonLink } from '../../types/uiTypes';

export type { PhotoPersonLink };

interface ClusterQuickActionProps {
    people: PhotoPersonLink[];
}

// Photos can carry zero, one, or many identified people. One resolves to a
// direct link; more than one needs a picker, rendered via a portal so it
// isn't clipped by the photo card's `overflow: hidden` (used for the
// thumbnail's rounded corners).
const ClusterQuickAction: React.FC<ClusterQuickActionProps> = ({ people }) => {
    const [open, setOpen] = useState(false);
    const [menuPos, setMenuPos] = useState<{ top: number; left: number } | null>(null);
    const triggerRef = useRef<HTMLButtonElement | null>(null);
    const menuRef = useRef<HTMLDivElement | null>(null);

    useLayoutEffect(() => {
        if (!open || !triggerRef.current) {
            return;
        }
        const rect = triggerRef.current.getBoundingClientRect();
        setMenuPos({ top: rect.bottom + 8, left: Math.max(8, rect.right - 220) });
    }, [open]);

    useEffect(() => {
        if (!open) {
            return undefined;
        }
        const close = () => setOpen(false);
        const handlePointerDown = (event: PointerEvent) => {
            const target = event.target as Node;
            if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) {
                return;
            }
            close();
        };
        const handleKeyDown = (event: KeyboardEvent) => {
            if (event.key === 'Escape') {
                close();
            }
        };
        document.addEventListener('pointerdown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        // Anchored via getBoundingClientRect at open time, so let scroll/resize
        // just close it rather than tracking position continuously.
        window.addEventListener('scroll', close, true);
        window.addEventListener('resize', close);
        return () => {
            document.removeEventListener('pointerdown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
            window.removeEventListener('scroll', close, true);
            window.removeEventListener('resize', close);
        };
    }, [open]);

    if (people.length === 0) {
        return null;
    }

    if (people.length === 1) {
        const [person] = people;
        return (
            <Link
                to={`/people/${encodeURIComponent(person.personId)}`}
                className="tile-quick-action"
                title={`View ${person.name || 'this person'} in People`}
                aria-label={`View ${person.name || 'this person'} in People`}
            >
                <UsersIcon className="tile-quick-action-icon" aria-hidden="true" />
            </Link>
        );
    }

    return (
        <>
            <button
                ref={triggerRef}
                type="button"
                className="tile-quick-action"
                title="View people in this photo"
                aria-label="View people in this photo"
                aria-haspopup="menu"
                aria-expanded={open}
                onClick={() => setOpen((prev) => !prev)}
            >
                <UsersIcon className="tile-quick-action-icon" aria-hidden="true" />
            </button>
            {open && menuPos && createPortal(
                <div
                    ref={menuRef}
                    className="gallery-menu tile-quick-action-menu"
                    role="menu"
                    style={{ position: 'fixed', top: menuPos.top, left: menuPos.left }}
                >
                    <p className="gallery-menu-label">People in this photo</p>
                    {people.map((person, index) => (
                        <Link
                            key={person.personId}
                            to={`/people/${encodeURIComponent(person.personId)}`}
                            className="gallery-menu-action"
                            role="menuitem"
                            onClick={() => setOpen(false)}
                        >
                            <UsersIcon className="toolbar-icon" aria-hidden="true" />
                            <span>{person.name || `Unnamed person ${index + 1}`}</span>
                        </Link>
                    ))}
                </div>,
                document.body,
            )}
        </>
    );
};

interface PhotoQuickActionsProps {
    // Omit on the page the action would just navigate back to itself.
    libraryHref?: string;
    workbenchHref?: string;
    people?: PhotoPersonLink[];
    className?: string;
}

// Hover-revealed corner icons that jump from wherever a photo tile is shown
// (Albums, Tools overview, Corrupted uploads, ...) into the other contexts a
// user might want next: the full library, the browser workbench, or the
// person this photo is clustered under.
const PhotoQuickActions: React.FC<PhotoQuickActionsProps> = ({
    libraryHref,
    workbenchHref,
    people = [],
    className = '',
}) => {
    if (!libraryHref && !workbenchHref && people.length === 0) {
        return null;
    }

    return (
        <div
            className={`tile-quick-actions ${className}`.trim()}
            onClick={(event) => event.stopPropagation()}
        >
            {libraryHref && (
                <Link
                    to={libraryHref}
                    className="tile-quick-action"
                    title="View in Library"
                    aria-label="View in Library"
                >
                    <PhotoIcon className="tile-quick-action-icon" aria-hidden="true" />
                </Link>
            )}
            {workbenchHref && (
                <Link
                    to={workbenchHref}
                    className="tile-quick-action"
                    title="Open in Workbench"
                    aria-label="Open in Workbench"
                >
                    <WrenchScrewdriverIcon className="tile-quick-action-icon" aria-hidden="true" />
                </Link>
            )}
            <ClusterQuickAction people={people} />
        </div>
    );
};

export const libraryFocusHref = (filename: string): string => `/?focus=${encodeURIComponent(filename)}`;
export const workbenchFilenameHref = (filename: string): string => `/tools/browser-workbench?filename=${encodeURIComponent(filename)}`;

export default PhotoQuickActions;
