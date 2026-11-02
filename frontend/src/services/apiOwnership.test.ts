import { expect, it } from 'vitest';
import { isArchiveRequest, resolveServiceUrl } from './apiOwnership';

it.each([
    ['/api/library/mine', 'GET'], ['/api/library/members', 'GET'],
    ['/api/library/switch', 'POST'], ['/api/library/rename', 'POST'],
    ['/api/library/invite', 'POST'], ['/api/library/invite/info?token=x', 'GET'],
    ['/api/library/invite/accept', 'POST'], ['/api/library/invite/revoke', 'POST'],
    ['/api/library/members/remove', 'POST'], ['/api/library/leave', 'POST'],
    ['/api/photos/trash', 'GET'], ['/api/albums', 'GET'],
])('keeps management on core: %s %s', (path, method) => {
    expect(isArchiveRequest(path, method)).toBe(false);
    expect(resolveServiceUrl(path, method, 'https://core/', 'https://archive/')).toBe(`https://core${path}`);
});

it.each([
    ['/api/library', 'DELETE'], ['/api/library/', 'delete'],
    ['/api/library/clean/request', 'POST'], ['/api/library/clean/confirm', 'POST'],
    ['/api/library/clean/status?jobId=x', 'GET'], ['/api/library/cleanup-info/', 'GET'],
    ['/api/library/download/request', 'POST'], ['/api/library/download/status', 'GET'],
    ['/api/library/export/manifest?cursor=x', 'GET'],
    ['/public/albums/token', 'GET'], ['/api/public/photos/token/thumbnail/a.jpg', 'GET'],
])('keeps operations/public on archive: %s %s', (path, method) => {
    expect(isArchiveRequest(path, method)).toBe(true);
    expect(resolveServiceUrl(path, method, 'https://core/', 'https://archive/')).toBe(`https://archive${path}`);
});

it('handles origin fallbacks, relative paths and preserves absolute URLs', () => {
    expect(resolveServiceUrl('api/library/mine', 'GET', 'https://core', '')).toBe('https://core/api/library/mine');
    expect(resolveServiceUrl('/api/library/export/manifest', 'GET', 'https://core', '')).toBe('https://core/api/library/export/manifest');
    expect(resolveServiceUrl('/api/library/mine', 'GET', '', '')).toBe('/api/library/mine');
    expect(resolveServiceUrl('https://other/path?x=y', 'GET', 'core', 'archive')).toBe('https://other/path?x=y');
    expect(resolveServiceUrl('//other/path', 'GET', 'core', 'archive')).toBe('//other/path');
    expect(isArchiveRequest('/api/library/cleanliness')).toBe(false);
    expect(isArchiveRequest('/publicity')).toBe(false);
    expect(isArchiveRequest('/api/library', 'GET')).toBe(false);
});
