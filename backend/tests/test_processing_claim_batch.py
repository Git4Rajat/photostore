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
