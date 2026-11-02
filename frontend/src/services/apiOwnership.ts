// Management lives on core; whole-library operations and public visitors use archive.
export const isArchiveRequest = (url: string, method = 'GET'): boolean => {
    const path = url.split(/[?#]/, 1)[0].replace(/^\/?/, '/').replace(/\/+$/, '');
    const under = (prefix: string) => path === prefix || path.startsWith(`${prefix}/`);
    return under('/public') || under('/api/public')
        || under('/api/library/clean') || under('/api/library/cleanup-info')
        || under('/api/library/download') || under('/api/library/export')
        || (path === '/api/library' && method.toUpperCase() === 'DELETE');
};

export const resolveServiceUrl = (
    url: string, method: string, coreBase: string, archiveBase: string,
): string => {
    if (/^(?:https?:)?\/\//i.test(url)) return url;
    const base = isArchiveRequest(url, method) ? (archiveBase || coreBase) : coreBase;
    if (!base) return url;
    return `${base.replace(/\/$/, '')}/${url.replace(/^\/+/, '')}`;
};
