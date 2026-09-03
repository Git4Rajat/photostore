"""Unit tests for the batch lease-claim endpoint (/upload/processing/claim-batch).

The pending-drain loop's lanes each used to call /upload/processing/claim
individually right before starting on an item -- fine at concurrency 1 (the
default), but a burst of N separate round trips when Turbo mode runs N lanes
at once. This collapses that initial burst into one call while keeping each
item's claim fully independent (own read/write, own success/failure) --
see _claim_processing_lease_response, which both the single-item and batch
routes now share.
"""
from __future__ import annotations

import pytest

import app
import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeMetadataTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        return dict(self.rows[key])


def _seed_row(metadata: _FakeMetadataTable, user_id: str, filename: str, **overrides) -> None:
    row = {
        'PartitionKey': user_id,
        'RowKey': filename,
        'processing_metadata': '{}',
        **overrides,
    }
    metadata.upsert_entity(row)


@pytest.fixture
def metadata(monkeypatch):
    table = _FakeMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    # _resolve_media_blob_name (used by the thumbnail-URL fallback path) reads
    # app's own module-level metadata_table_client, a separate reference from
    # storage_utils._CTX above.
    monkeypatch.setattr(app, 'metadata_table_client', table)
    yield table


def test_claim_response_helper_claims_each_item_independently(metadata):
    _seed_row(metadata, 'lib-A', 'a.jpg', ocr_status='pending')
    _seed_row(metadata, 'lib-A', 'b.jpg', ocr_status='pending')

    response_a, status_a = app._claim_processing_lease_response('lib-A', 'a.jpg', 'lane-1', ['ocr'])
    response_b, status_b = app._claim_processing_lease_response('lib-A', 'b.jpg', 'lane-1', ['ocr'])

    assert status_a == status_b == 200
    assert response_a['claimed'] is True
    assert response_b['claimed'] is True
    assert metadata.get_entity('lib-A', 'a.jpg')['ocr_status'] == 'running'
    assert metadata.get_entity('lib-A', 'b.jpg')['ocr_status'] == 'running'


def test_claim_response_helper_reports_lease_conflict_without_raising(metadata):
    _seed_row(metadata, 'lib-A', 'a.jpg', ocr_status='pending')
    storage_utils.claim_processing_lease('lib-A', 'a.jpg', 'other-tab', steps=['ocr'])

    response, status = app._claim_processing_lease_response('lib-A', 'a.jpg', 'lane-1', ['ocr'])

    assert response['claimed'] is False
    assert response['reason'] == 'lease_active'
    assert status == 200


def test_batch_route_claims_multiple_photos_in_one_call(metadata, monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('lib-A', None))
    _seed_row(metadata, 'lib-A', 'a.jpg', ocr_status='pending')
    _seed_row(metadata, 'lib-A', 'b.jpg', ocr_status='pending')
    _seed_row(metadata, 'lib-A', 'c.jpg', ocr_status='pending')

    with app.app.test_request_context(
        '/upload/processing/claim-batch',
        method='POST',
        json={'items': [
            {'filename': 'a.jpg', 'leaseId': 'lane-1', 'steps': ['ocr']},
            {'filename': 'b.jpg', 'leaseId': 'lane-2', 'steps': ['ocr']},
            {'filename': 'c.jpg', 'leaseId': 'lane-3', 'steps': ['ocr']},
        ]},
    ):
        response = app.upload_processing_claim_batch()

    body = response.get_json()
    assert [item['filename'] for item in body['results']] == ['a.jpg', 'b.jpg', 'c.jpg']
    assert all(item['claimed'] for item in body['results'])
    assert metadata.get_entity('lib-A', 'a.jpg')['processing_lease_owner'] == 'lane-1'
    assert metadata.get_entity('lib-A', 'b.jpg')['processing_lease_owner'] == 'lane-2'
    assert metadata.get_entity('lib-A', 'c.jpg')['processing_lease_owner'] == 'lane-3'


def test_batch_route_isolates_one_failed_claim_from_the_rest(metadata, monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('lib-A', None))
    _seed_row(metadata, 'lib-A', 'a.jpg', ocr_status='pending')
    _seed_row(metadata, 'lib-A', 'b.jpg', ocr_status='pending')
    # Someone else already holds b.jpg's lease.
    storage_utils.claim_processing_lease('lib-A', 'b.jpg', 'other-tab', steps=['ocr'])

    with app.app.test_request_context(
        '/upload/processing/claim-batch',
        method='POST',
        json={'items': [
            {'filename': 'a.jpg', 'leaseId': 'lane-1', 'steps': ['ocr']},
            {'filename': 'b.jpg', 'leaseId': 'lane-2', 'steps': ['ocr']},
        ]},
    ):
        response = app.upload_processing_claim_batch()

    body = response.get_json()
    results_by_name = {item['filename']: item for item in body['results']}
    assert results_by_name['a.jpg']['claimed'] is True
    assert results_by_name['b.jpg']['claimed'] is False
    assert results_by_name['b.jpg']['reason'] == 'lease_active'


def test_batch_route_rejects_empty_items(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('lib-A', None))
    with app.app.test_request_context('/upload/processing/claim-batch', method='POST', json={'items': []}):
        response, status = app.upload_processing_claim_batch()
    assert status == 400


def test_batch_route_rejects_too_many_items(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('lib-A', None))
    too_many = [{'filename': f'{i}.jpg'} for i in range(app.MAX_CLAIM_BATCH_ITEMS + 1)]
    with app.app.test_request_context('/upload/processing/claim-batch', method='POST', json={'items': too_many}):
        response, status = app.upload_processing_claim_batch()
    assert status == 400


def test_claim_thumbnail_url_prefers_client_echoed_blob_name_over_row_lookup(metadata, monkeypatch):
    # Same-filename race this guards against: two files named 'a.jpg' upload
    # around the same time. The row (looked up by filename alone) may already
    # reflect the OTHER file's anonymousImageId by the time this claim call
    # for THIS file's just-finalized upload fires. Trusting a validated
    # client-echoed blobName (this file's own /upload/init response) instead
    # of re-deriving from the shared row is what stops the direct thumbnail
    # PUT from landing on a different photo's already-correct thumbnail blob.
    _seed_row(metadata, 'lib-A', 'a.jpg', thumbnail_status='pending', anonymousImageId='row-owned-uuid')
    captured = []
    monkeypatch.setattr(
        app, '_create_direct_thumbnail_upload_blob_url',
        lambda physical_name: captured.append(physical_name) or ('https://example.test/thumb', '2099-01-01T00:00:00Z'),
    )
    client_uuid = '550e8400-e29b-41d4-a716-446655440000'

    response, status = app._claim_processing_lease_response('lib-A', 'a.jpg', 'lane-1', ['thumbnail'], client_uuid)

    assert status == 200
    assert response['claimed'] is True
    assert captured == [client_uuid]


def test_claim_thumbnail_url_falls_back_to_row_lookup_without_a_valid_client_blob_name(metadata, monkeypatch):
    _seed_row(metadata, 'lib-A', 'a.jpg', thumbnail_status='pending', anonymousImageId='row-owned-uuid')
    _seed_row(metadata, 'lib-A', 'b.jpg', thumbnail_status='pending', anonymousImageId='row-owned-uuid-2')
    captured = []
    monkeypatch.setattr(
        app, '_create_direct_thumbnail_upload_blob_url',
        lambda physical_name: captured.append(physical_name) or ('https://example.test/thumb', '2099-01-01T00:00:00Z'),
    )

    # No blobName at all (older client / non-upload reprocessing caller).
    response_no_blob_name, _ = app._claim_processing_lease_response('lib-A', 'a.jpg', 'lane-1', ['thumbnail'], None)
    # A malformed/untrusted blobName on a different photo -- must not be
    # honored, same rejection _validate_client_blob_name applies at finalize.
    response_bad_blob_name, _ = app._claim_processing_lease_response('lib-A', 'b.jpg', 'lane-2', ['thumbnail'], 'not-a-uuid')

    assert response_no_blob_name['claimed'] is True
    assert response_bad_blob_name['claimed'] is True
    assert captured == ['row-owned-uuid', 'row-owned-uuid-2']


def test_batch_route_passes_each_items_own_blob_name_through(metadata, monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('lib-A', None))
    _seed_row(metadata, 'lib-A', 'a.jpg', anonymousImageId='row-a-uuid')
    _seed_row(metadata, 'lib-A', 'b.jpg', anonymousImageId='row-b-uuid')
    captured = []
    monkeypatch.setattr(
        app, '_create_direct_thumbnail_upload_blob_url',
        lambda physical_name: captured.append(physical_name) or ('https://example.test/thumb', '2099-01-01T00:00:00Z'),
    )

    with app.app.test_request_context(
        '/upload/processing/claim-batch',
        method='POST',
        json={'items': [
            {'filename': 'a.jpg', 'leaseId': 'lane-1', 'blobName': '550e8400-e29b-41d4-a716-446655440000'},
            {'filename': 'b.jpg', 'leaseId': 'lane-2'},
        ]},
    ):
        response = app.upload_processing_claim_batch()

    body = response.get_json()
    assert [item['claimed'] for item in body['results']] == [True, True]
    assert captured == ['550e8400-e29b-41d4-a716-446655440000', 'row-b-uuid']
