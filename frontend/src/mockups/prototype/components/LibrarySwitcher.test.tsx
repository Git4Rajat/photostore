import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { LibrarySwitcher } from './LibrarySwitcher';
import * as library from '../../../services/libraryClient';

vi.mock('../../../services/libraryClient', () => ({ getMine: vi.fn(), switchLibrary: vi.fn(), leaveLibrary: vi.fn() }));
vi.mock('../../../services/clientDataReset', () => ({ clearLibraryClientData: vi.fn() }));

const mine = (libraries: Array<{ libraryId: string; name: string; isOwner?: boolean }>) => ({
    activeLibraryId: 'a',
    maxMembers: 15,
    libraries: libraries.map((l) => ({ ownerUserId: 'u', isOwner: true, ...l })),
});

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('LibrarySwitcher', () => {
    it('renders nothing for a single-library user', async () => {
        vi.mocked(library.getMine).mockResolvedValue(mine([{ libraryId: 'a', name: 'Mine' }]));
        const { container } = render(<LibrarySwitcher reload={vi.fn()} />);
        await waitFor(() => expect(library.getMine).toHaveBeenCalled());
        expect(container.innerHTML).toBe('');
    });

    it('lists libraries, marks the active one and switches then reloads', async () => {
        vi.mocked(library.getMine).mockResolvedValue(mine([{ libraryId: 'a', name: 'Mine' }, { libraryId: 'b', name: 'Family' }]));
        vi.mocked(library.switchLibrary).mockResolvedValue('b');
        const reload = vi.fn();
        render(<LibrarySwitcher reload={reload} />);
        expect(await screen.findByText('Mine ✓')).toBeTruthy();
        fireEvent.click(screen.getByText('Family'));
        await waitFor(() => expect(reload).toHaveBeenCalled());
        expect(library.switchLibrary).toHaveBeenCalledWith('b');
    });

    it('does nothing when the active library is chosen, and shows an error on failure', async () => {
        vi.mocked(library.getMine).mockResolvedValue(mine([{ libraryId: 'a', name: 'Mine' }, { libraryId: 'b', name: 'Family' }]));
        vi.mocked(library.switchLibrary).mockRejectedValue(new Error('nope'));
        const reload = vi.fn();
        render(<LibrarySwitcher reload={reload} />);
        fireEvent.click(await screen.findByText('Mine ✓'));
        expect(library.switchLibrary).not.toHaveBeenCalled();
        fireEvent.click(screen.getByText('Family'));
        expect((await screen.findByRole('alert')).textContent).toBe('nope');
        expect(reload).not.toHaveBeenCalled();
    });

    it('offers Leave only for libraries the user does not own, after a confirmation', async () => {
        vi.mocked(library.getMine).mockResolvedValue(mine([{ libraryId: 'a', name: 'Mine' }, { libraryId: 'b', name: 'Family', isOwner: false }]));
        vi.mocked(library.leaveLibrary).mockResolvedValue('a');
        const reload = vi.fn();
        render(<LibrarySwitcher reload={reload} />);
        await screen.findByText('Mine ✓');
        expect(screen.getAllByText(/^Leave /)).toHaveLength(1);
        fireEvent.click(screen.getByText('Leave Family'));
        expect(library.leaveLibrary).not.toHaveBeenCalled();
        fireEvent.click(screen.getByText('Leave library'));
        await waitFor(() => expect(reload).toHaveBeenCalled());
        expect(library.leaveLibrary).toHaveBeenCalledWith('b');
    });

    it('cancelling the confirmation leaves nothing changed', async () => {
        vi.mocked(library.getMine).mockResolvedValue(mine([{ libraryId: 'a', name: 'Mine' }, { libraryId: 'b', name: 'Family', isOwner: false }]));
        render(<LibrarySwitcher reload={vi.fn()} />);
        fireEvent.click(await screen.findByText('Leave Family'));
        fireEvent.click(screen.getByText('Cancel'));
        expect(screen.getByText('Leave Family')).toBeTruthy();
        expect(library.leaveLibrary).not.toHaveBeenCalled();
    });
});
