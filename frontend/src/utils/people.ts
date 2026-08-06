import type { PersonSummary } from '../types/people';

// A person/cluster is "named" when it has a real label, i.e. it isn't the
// "Unnamed N" placeholder (mirrors the backend `_is_unnamed_name`).
export const isNamedLabel = (name?: string | null): boolean => {
    const trimmed = (name || '').trim();
    return Boolean(trimmed) && !/^unnamed\s*\d*$/i.test(trimmed);
};

export const isNamedPerson = (p: PersonSummary): boolean => isNamedLabel(p.name);

// Named first, unnamed last; alphabetical by name within each group.
export const compareNamedFirstThenAlpha = (aName?: string | null, bName?: string | null): number => {
    const aNamed = isNamedLabel(aName);
    const bNamed = isNamedLabel(bName);
    if (aNamed !== bNamed) return aNamed ? -1 : 1;
    return (aName || '').localeCompare(bName || '', undefined, { sensitivity: 'base' });
};
