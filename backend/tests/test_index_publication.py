from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

import index_publication as publication
from index_work import WorkStore
from test_index_work import EtagTable


@pytest.fixture
def lease():
    now = [1000.0]
    store = WorkStore(EtagTable(), clock=lambda: now[0])
    key = store.request('A', 'people')
    return store, store.claim(key), now


def test_existing_blob_publication_is_conditional(lease):
    store, claim, _ = lease
    client = SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(etag='v1'), upload_blob=Mock())
    with publication.publication(store, claim):
        publication.fenced(client).upload_blob(b'data', overwrite=True)
    assert client.upload_blob.call_args.kwargs['etag'] == 'v1'
    assert client.upload_blob.call_args.kwargs['match_condition'] == MatchConditions.IfNotModified


def test_new_blob_uses_create_only(lease):
    store, claim, _ = lease
    client = SimpleNamespace(get_blob_properties=Mock(side_effect=ResourceNotFoundError('missing')), upload_blob=Mock())
    with publication.publication(store, claim):
        publication.fenced(client).upload_blob(b'data', overwrite=True)
    assert client.upload_blob.call_args.kwargs['overwrite'] is False


def test_lost_lease_blocks_publication_before_any_blob_write(lease):
    store, claim, now = lease
    client = SimpleNamespace(upload_blob=Mock())
    with pytest.raises(RuntimeError, match='lease lost'):
        with publication.publication(store, claim):
            now[0] += 181
            publication.fenced(client).upload_blob(b'data')
    client.upload_blob.assert_not_called()


@pytest.mark.parametrize('stage', ['properties', 'upload'])
def test_swallowed_publisher_failure_still_prevents_acknowledgement(lease, stage):
    store, claim, _ = lease
    client = SimpleNamespace(
        get_blob_properties=Mock(return_value=SimpleNamespace(etag='v1')),
        upload_blob=Mock(side_effect=ResourceModifiedError('conflict')),
    )
    if stage == 'properties':
        client.get_blob_properties.side_effect = OSError('storage unavailable')
    with pytest.raises(RuntimeError, match='lease lost'):
        with publication.publication(store, claim):
            try:
                publication.fenced(client).upload_blob(b'data')
            except Exception:
                pass
            publication.check()


def test_existing_search_manifest_cas_is_not_replaced(lease):
    store, claim, _ = lease
    client = SimpleNamespace(upload_blob=Mock(), get_blob_properties=Mock(side_effect=AssertionError('extra read')))
    with publication.publication(store, claim):
        publication.fenced(client).upload_blob(
            b'data', etag='search-v1', match_condition=MatchConditions.IfNotModified)
    assert client.upload_blob.call_args.kwargs['etag'] == 'search-v1'
