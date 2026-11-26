import React, { useEffect, useState } from 'react';
import * as library from '../../../services/libraryClient';
import { clearLibraryClientData } from '../../../services/clientDataReset';

type Summary = Awaited<ReturnType<typeof library.getMine>>['libraries'][number];

// Account-menu section that lists the libraries the signed-in user belongs to and
// switches the active one. Renders nothing for a single-library user. The server
// remembers the last-used library (switch persists it), so the next sign-in lands there.
export const LibrarySwitcher: React.FC<{
    onSwitched?: () => void;
    // Swappable for tests; a switch needs a full reload so every in-memory store rebootstraps.
    reload?: () => void;
}> = ({ onSwitched, reload = () => window.location.assign('/') }) => {
    const [libraries, setLibraries] = useState<Summary[]>([]);
    const [activeId, setActiveId] = useState('');
    const [busyId, setBusyId] = useState('');
    const [error, setError] = useState('');
    const [leavingId, setLeavingId] = useState('');

    useEffect(() => {
        let live = true;
        library.getMine()
            .then((mine) => {
                if (!live) return;
                setLibraries(mine.libraries ?? []);
                setActiveId(mine.activeLibraryId);
            })
            .catch(() => { /* no switcher on a failed lookup */ });
        return () => { live = false; };
    }, []);

    if (libraries.length < 2) return null;

    const choose = async (id: string) => {
        if (id === activeId || busyId) return;
        setBusyId(id);
        setError('');
        try {
            await library.switchLibrary(id);
            // Cached photos/people/albums belong to the old library.
            clearLibraryClientData();
            onSwitched?.();
            reload();
        } catch (err) {
            setBusyId('');
            setError(err instanceof Error ? err.message : 'Could not switch libraries.');
        }
    };

    const leave = async (id: string) => {
        if (busyId) return;
        setBusyId(id);
        setError('');
        try {
            // The server drops the user back into their own library.
            await library.leaveLibrary(id);
            clearLibraryClientData();
            onSwitched?.();
            reload();
        } catch (err) {
            setBusyId('');
            setError(err instanceof Error ? err.message : 'Could not leave that library.');
        }
    };

    const leaving = libraries.find((lib) => lib.libraryId === leavingId);

    return (
        <>
            <div className="pt-account-sep" />
            <div className="pt-account-libs" role="group" aria-label="Libraries">
                <span className="pt-account-libs-title">Libraries</span>
                {libraries.map((lib) => (
                    <button
                        key={lib.libraryId}
                        type="button"
                        aria-current={lib.libraryId === activeId ? 'true' : undefined}
                        disabled={Boolean(busyId)}
                        onClick={() => void choose(lib.libraryId)}
                    >
                        {lib.name || 'Library'}{lib.libraryId === activeId ? ' ✓' : busyId === lib.libraryId ? '…' : ''}
                    </button>
                ))}
                {leaving ? (
                    <div className="pt-account-libs-confirm">
                        <span>Leave “{leaving.name || 'Library'}”? You’ll lose access to its photos. Photos you uploaded stay in it.</span>
                        <button type="button" disabled={Boolean(busyId)} onClick={() => void leave(leaving.libraryId)}>Leave library</button>
                        <button type="button" disabled={Boolean(busyId)} onClick={() => setLeavingId('')}>Cancel</button>
                    </div>
                ) : libraries.filter((lib) => !lib.isOwner).map((lib) => (
                    <button key={`leave-${lib.libraryId}`} type="button" className="pt-account-libs-leave" onClick={() => setLeavingId(lib.libraryId)}>
                        Leave {lib.name || 'library'}
                    </button>
                ))}
                {error && <span role="alert" className="pt-account-libs-error">{error}</span>}
            </div>
        </>
    );
};
