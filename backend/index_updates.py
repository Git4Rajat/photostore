"""Explicit index-maintenance boundaries shared by producers and workers.

These scopes constrain the legacy publishers; they do not make whole-file
publishers bounded. Delta-based publication is a separate migration.
"""

SCOPE_KINDS = {
    'full': ('sort', 'access', 'albums', 'people'),
    'light': ('sort', 'access'),
    'people': ('people',),
    'albums': ('albums',),
    'sort': ('sort',),
    'access': ('access',),
    'search': (),
    'compact': (),
    'reconcile': (),
    'vector': ('vector',),
    'tag_embedding': ('tag_embedding',),
    'people_embedding': ('people_embedding',),
    'dedup': (),
    'dedup-idle': (),
}

SEARCH_SCOPES = frozenset(('light', 'people', 'search'))

# Ensuring an existing projection does not constitute another source change.
READ_REASONS = frozenset((
    '', 'client', 'client-light', 'serving-process', 'trigger',
    'people-index-missing', 'no-search-db', 'search-db-no-base',
    'sort-dirty', 'access-dirty', 'no-access-index', 'schema-upgrade',
    'upload-hash-snapshot',
))


def validate_scope(scope: str) -> str:
    if scope not in SCOPE_KINDS:
        raise ValueError('Unknown index maintenance scope')
    return scope


def scope_for_index(kind: str) -> str:
    scope = 'search' if kind == 'lexical' else kind
    return validate_scope(scope)
