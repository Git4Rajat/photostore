"""Pin the reusable blob fake's create, revision and conditional read/write contract."""
import io

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from tests.blob_fakes import FakeBlobServiceClient


def test_missing_blob_and_create_conflict():
    blob = FakeBlobServiceClient().get_blob_client('index', 'manifest')
    for operation in (blob.get_blob_properties, blob.download_blob, blob.delete_blob):
        with pytest.raises(ResourceNotFoundError):
            operation()
    blob.upload_blob(b'first', overwrite=False)
    with pytest.raises(ResourceExistsError):
        blob.upload_blob(b'second', overwrite=False)
    assert blob.download_blob().readall() == b'first'


@pytest.mark.parametrize('operation', ['upload_blob', 'download_blob', 'delete_blob'])
def test_stale_etag_rejected_across_clients_even_for_identical_payload(operation):
    service = FakeBlobServiceClient()
    blob = service.get_blob_client('index', 'manifest')
    blob.upload_blob(b'same')
    props = blob.get_blob_properties()
    other = service.get_blob_client('index', 'manifest')
    other.upload_blob(b'same', overwrite=True)
    assert other.get_blob_properties().etag != props.etag
    options = {'etag': props.etag, 'match_condition': MatchConditions.IfNotModified}
    with pytest.raises(ResourceModifiedError):
        if operation == 'upload_blob':
            blob.upload_blob(b'lost update', overwrite=True, **options)
        else:
            getattr(blob, operation)(**options)
    assert other.download_blob().readall() == b'same'


def test_current_etag_allows_read_and_update_but_cannot_survive_delete_recreate():
    blob = FakeBlobServiceClient().get_blob_client('index', 'manifest')
    blob.upload_blob(io.BytesIO(b'first'))
    props = blob.get_blob_properties()
    options = {'etag': props.etag, 'match_condition': MatchConditions.IfNotModified}
    assert props.size == 5
    assert blob.download_blob(**options).readall() == b'first'
    blob.upload_blob(b'next', overwrite=True, **options)
    assert blob.get_blob_properties().etag != props.etag
    blob.delete_blob(etag=blob.get_blob_properties().etag, match_condition=MatchConditions.IfNotModified)
    blob.upload_blob(b'first')
    with pytest.raises(ResourceModifiedError):
        blob.download_blob(**options)