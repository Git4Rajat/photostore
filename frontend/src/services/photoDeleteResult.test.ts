import { expect, it } from 'vitest';
import { failedDeleteIds } from './photoDeleteResult';

it('returns only the photos the server could not soft delete, in request order', () => {
    const errors = ['b.jpg: metadata: soft-delete failed', 'ghost.jpg: Not found', 'd.jpg: trash index: retry to list it in Recently Deleted', 'a.jpg: metadata: soft-delete failed'];
    expect(failedDeleteIds(['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg'], errors)).toEqual(['a.jpg', 'b.jpg']);
});

it('treats a clean or missing error list as no failures', () => {
    expect(failedDeleteIds(['a.jpg'], [])).toEqual([]);
    expect(failedDeleteIds(['a.jpg'], undefined)).toEqual([]);
});

it('keeps filenames that contain the separator intact', () => {
    expect(failedDeleteIds(['a: b.jpg'], ['a: b.jpg: metadata: soft-delete failed'])).toEqual(['a: b.jpg']);
});
