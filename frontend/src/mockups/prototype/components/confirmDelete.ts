import { confirmDialog } from '../../../components/shared/dialogs';
import { plural } from '../../../utils/format';

/** Asks before moving items to Recently Deleted. "Items", not "photos": a
 *  selection can include videos, and they go the same way. */
export const confirmDeleteItems = (count: number): Promise<boolean> => confirmDialog({
    title: `Delete ${plural(count, 'item')}?`,
    message: count === 1
        ? 'It moves to Recently Deleted, where you can restore it for 30 days.'
        : 'They move to Recently Deleted, where you can restore them for 30 days.',
    confirmLabel: 'Delete',
    danger: true,
});
