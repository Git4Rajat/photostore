"""Explicit index-maintenance boundaries shared by producers and workers.

These scopes constrain the legacy publishers; they do not make whole-file
publishers bounded. Delta-based publication is a separate migration.
"""
import os

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
    'lexical_artifact': (),
    'dedup': (),
    'dedup-idle': (),
}

# Nothing on a serving path reads the photo-vector artifact any more (semantic
# scoring left /api/photos/search), and its full rebuild cost ~20 s twice per
# wave. Keep it manual-only (admin repair) unless explicitly re-enabled.
VECTOR_INDEX_AUTOMATIC = os.getenv('VECTOR_INDEX_AUTOMATIC', '').strip().lower() in ('1', 'true', 'yes')

SEARCH_SCOPES = frozenset(('light', 'people', 'search'))

# Scopes whose SCOPE_KINDS mapping is a single bounded, already-cheap kind --
# safe for inline_index_merge.py to attempt in `core` via
# index_ownership.allow_inline_merge. The single source of truth for this
# set; inline_index_merge.py, index_ownership.py, and app.py's
# dispatch_scoped_rebuild all reference this constant instead of their own
# copies, so there is nowhere left for the three to drift apart.
INLINE_MERGE_SCOPES = ('people', 'albums', 'sort', 'access', 'search')

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
    scope = {'lexical': 'search', 'hash': 'dedup'}.get(kind, kind)
    return validate_scope(scope)
