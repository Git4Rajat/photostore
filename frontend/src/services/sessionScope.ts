import * as auth from './passwordAuthClient';

/** Shared by the edit queue and HTTP retries; excludes token renewal within one session. */
export const sessionScope = (): string => JSON.stringify([
    ('getSubjectFromToken' in auth ? auth.getSubjectFromToken() : '') || '__local__',
    auth.getActiveLibraryFromToken() || '__default__',
]);
