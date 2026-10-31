"""Execution boundary for published catalog artifacts, independent of queues.

Storage/source mutations and the internal clustering FAISS runtime are not
catalog publication. Vision's model-owned tag artifact is an explicit exception.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os


class IndexOwnershipError(RuntimeError):
    """A non-publisher process attempted a catalog build/publication."""


def configured_build_roles():
    roles = {role.strip() for role in os.getenv('INDEX_BUILD_ROLES', 'index-worker').split(',')
             if role.strip()}
    if roles - {'index-worker'}:
        raise ValueError('INDEX_BUILD_ROLES may only contain index-worker; '
                         'cluster and vision cannot publish catalogs')
    return roles


# A narrow, explicit exception to the index-worker-only rule above -- NOT a
# change to configured_build_roles()/index_build_allowed(). Lets `core`
# publish ONLY the bounded, already-cheap merge scopes (see
# inline_index_merge.py) when an eligibility check it already passed proves
# this call will take the targeted merge path, never a full/embedding-heavy
# rebuild. The allowed-scope list here is the only thing standing between
# that and the OOM incident index_build_allowed() exists to prevent
# (storage_utils.py's comment near _ROLE_MAY_BUILD_INDEXES) -- never widen it
# to 'full'/'vector'/'compact'/'people_embedding'/'tag_embedding'.
_INLINE_MERGE_SCOPE = ContextVar('inline_merge_scope', default=None)


@contextmanager
def allow_inline_merge(scope):
    import index_updates
    allowed = index_updates.INLINE_MERGE_SCOPES
    if scope not in allowed:
        raise ValueError(f'Inline merge is only permitted for {allowed}, got {scope!r}')
    # require_catalog_owner below only checks "is some inline-merge context
    # active", not which one -- it has no way to know which catalog_publisher
    # function is actually being invoked underneath it. This is the one
    # place that CAN check: the kinds this scope's caller is permitted to
    # touch (storage_utils.prime_all_user_indexes_sequentially's `kinds`
    # argument, derived from SCOPE_KINDS[scope]) must themselves all be
    # bounded scopes -- if SCOPE_KINDS[scope] is ever widened to include a
    # heavier kind (e.g. 'lexical'/'vector'), this raises here, before the
    # exception is granted, instead of silently letting that heavier
    # publisher run from `core`.
    if not set(index_updates.SCOPE_KINDS.get(scope, ())) <= set(allowed):
        raise ValueError(f'SCOPE_KINDS[{scope!r}] is not entirely within the bounded inline-merge '
                         f'scopes {allowed} -- refusing to grant the inline-merge exception')
    token = _INLINE_MERGE_SCOPE.set(scope)
    try:
        yield
    finally:
        _INLINE_MERGE_SCOPE.reset(token)


def require_catalog_owner(*, model_owned_tag=False):
    # Lazy import avoids the storage_utils/search_db dependency cycle. One
    # authority is also used by the existing read-side guards.
    import storage_utils
    if storage_utils.index_build_allowed():
        return
    if _INLINE_MERGE_SCOPE.get() is not None:
        return
    if model_owned_tag and os.getenv('APP_ROLE', 'core').strip().lower() == 'vision':
        import vision_utils
        if vision_utils.image_encoder_available():
            return
    raise IndexOwnershipError('Catalog publication requires APP_ROLE=index-worker '
                              '(Vision may publish its model-owned tag artifact, '
                              'or core may run an explicitly-scoped inline merge)')


def catalog_publisher(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        require_catalog_owner()
        return function(*args, **kwargs)
    return guarded


def model_tag_publisher(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        require_catalog_owner(model_owned_tag=True)
        return function(*args, **kwargs)
    return guarded
