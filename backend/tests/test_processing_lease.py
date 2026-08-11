"""Unit tests for the per-photo processing lease -- the mechanism ipworker now
plugs into (via claim_processing_lease/release_processing_lease in app.py's
_handle_ipwork_queue_payload) to compete fairly with browser tabs for the same
photo in 'both' processing mode, instead of both sides always doing the work
and only reconciling at write time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


def test_claim_marks_requested_steps_running(metadata):
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending', face_status='pending')

    lease = storage_utils.claim_processing_lease(
        user_id, filename, 'ipworker-job-1', steps=['ocr', 'face'],
    )

    assert lease['ownerId'] == 'ipworker-job-1'
    stored = metadata.get_entity(user_id, filename)
    assert stored['ocr_status'] == 'running'
    assert stored['face_status'] == 'running'
    assert stored['processing_lease_owner'] == 'ipworker-job-1'


def test_second_claim_by_different_owner_is_rejected_while_active(metadata):
    """This is exactly the 'both' mode race: the browser claims first, and
    ipworker's competing claim on the same photo must be refused so it never
    starts (wasted) inference the browser is already doing."""
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending')
    storage_utils.claim_processing_lease(user_id, filename, 'browser-tab-1', steps=['ocr'])

    with pytest.raises(RuntimeError, match='already held'):
        storage_utils.claim_processing_lease(user_id, filename, 'ipworker-job-1', steps=['ocr'])


def test_same_owner_can_reclaim_its_own_lease(metadata):
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending')
    storage_utils.claim_processing_lease(user_id, filename, 'ipworker-job-1', steps=['ocr'])

    # Renewal/retry by the same owner must not raise.
    lease = storage_utils.claim_processing_lease(user_id, filename, 'ipworker-job-1', steps=['ocr'])
    assert lease['ownerId'] == 'ipworker-job-1'


def test_release_lets_a_different_owner_claim_next(metadata):
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending')
    storage_utils.claim_processing_lease(user_id, filename, 'ipworker-job-1', steps=['ocr'])

    storage_utils.release_processing_lease(user_id, filename, 'ipworker-job-1')

    lease = storage_utils.claim_processing_lease(user_id, filename, 'browser-tab-1', steps=['ocr'])
    assert lease['ownerId'] == 'browser-tab-1'


def test_expired_lease_can_be_reclaimed_by_a_different_owner(metadata):
    user_id, filename = 'lib-A', 'photo.jpg'
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    _seed_row(
        metadata, user_id, filename,
        ocr_status='running',
        processing_lease_owner='ipworker-crashed-job',
        processing_lease_expires_at=expired_at,
    )

    lease = storage_utils.claim_processing_lease(user_id, filename, 'browser-tab-1', steps=['ocr'])
    assert lease['ownerId'] == 'browser-tab-1'


def test_claim_does_not_reset_already_terminal_steps_to_running(metadata):
    """A lease claim for ['ocr', 'face'] on a photo where face is already
    'done' must not flip it back to 'running' -- only steps still in flight
    get claimed, matching the write-time _step_locked_done guard's invariant
    that 'done' means done."""
    user_id, filename = 'lib-A', 'photo.jpg'
    _seed_row(metadata, user_id, filename, ocr_status='pending', face_status='done')

    storage_utils.claim_processing_lease(user_id, filename, 'ipworker-job-1', steps=['ocr', 'face'])

    stored = metadata.get_entity(user_id, filename)
    assert stored['ocr_status'] == 'running'
    assert stored['face_status'] == 'done'
