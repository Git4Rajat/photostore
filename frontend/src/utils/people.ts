// A person/cluster is "named" when it has a real label, i.e. it isn't the
// "Unnamed N" placeholder (mirrors the backend `_is_unnamed_name`).
export const isNamedLabel = (name?: string | null): boolean => {
    const trimmed = (name || '').trim();
    return Boolean(trimmed) && !/^unnamed\s*\d*$/i.test(trimmed);
};


