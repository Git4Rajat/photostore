"""Execution boundary for published catalog artifacts, independent of queues.

Storage/source mutations and the internal clustering FAISS runtime are not
catalog publication. Vision's model-owned tag artifact is an explicit exception.
"""
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


def require_catalog_owner(*, model_owned_tag=False):
    # Lazy import avoids the storage_utils/search_db dependency cycle. One
    # authority is also used by the existing read-side guards.
    import storage_utils
    if storage_utils.index_build_allowed():
        return
    if model_owned_tag and os.getenv('APP_ROLE', 'core').strip().lower() == 'vision':
        import vision_utils
        if vision_utils.image_encoder_available():
            return
    raise IndexOwnershipError('Catalog publication requires APP_ROLE=index-worker '
                              '(Vision may publish its model-owned tag artifact)')


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
