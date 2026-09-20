"""Regression test for the 2026-09-14 stuck-processing incident: 3 real
uploads on stcontainerapp-dv were left with every step permanently wedged at
'running' because _apply_client_processing_results only persists per-step
status via one final upsert, and nothing resets a step's status if an
exception is thrown before reaching it (release_processing_lease, called by
the caller's error path, only clears lease ownership, not step statuses).
"""
from __future__ import annotations

import pytest

import storage_utils


class _ResourceNotFound(Exception):
    pass


class _FakeMetadataTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        key = (entity['PartitionKey'], entity['RowKey'])
        if key not in self.rows:
            raise _ResourceNotFound(f'{key} not found')
        self.rows[key] = dict(entity)

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


def test_exception_mid_apply_unsticks_running_steps_instead_of_wedging_forever(metadata, monkeypatch):
    user_id, filename = 'owner', '_MG_1337.CR3'
    # Mirrors claim_processing_lease's effect right before ipworker runs its
    # steps: every requested step is marked 'running', lease is held.
    _seed_row(
        metadata, user_id, filename,
        preview_status='running', thumbnail_status='done', exif_status='running',
        ocr_status='running', ai_vision_status='running', map_detection_status='running',
        face_status='running',
        processing_lease_owner='ipworker-job-1',
        processing_lease='ipworker-job-1',
        processing_lease_expires_at='2026-09-12T18:46:59+00:00',
    )

    def _boom(*args, **kwargs):
        raise RuntimeError('semantic index rebuild exploded')

    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', _boom)

    with pytest.raises(RuntimeError, match='semantic index rebuild exploded'):
        storage_utils.apply_client_processing_results_for_file(
            user_id, filename,
            client_processing={'exif': {'hasData': True}},
            client_asset_id='ipworker:job-1',
            origin='ipworker',
            claimed_steps=['preview', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'],
        )

    stored = metadata.get_entity(user_id, filename)
    for step in ('preview', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face'):
        assert stored[f'{step}_status'] == 'failed', f'{step}_status should be retryable, not stuck running'
    # A step this call never claimed responsibility for is left alone.
    assert stored['thumbnail_status'] == 'done'
    # The lease is released too, so a fresh claim can pick this photo back up.
    assert stored['processing_lease_owner'] == ''
    assert stored['processing_lease_expires_at'] == ''


def test_exception_with_no_running_steps_does_not_touch_the_row(metadata, monkeypatch):
    """A crash on a photo where every claimed step already resolved (e.g. the
    exception happened after the real work, in something unrelated) must not
    spuriously flip already-terminal statuses to 'failed'."""
    user_id, filename = 'owner', 'photo.jpg'
    _seed_row(metadata, user_id, filename, exif_status='done', ocr_status='no_data')

    def _boom(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', _boom)

    with pytest.raises(RuntimeError):
        storage_utils.apply_client_processing_results_for_file(
            user_id, filename,
            client_processing={},
            client_asset_id='ipworker:job-2',
            origin='ipworker',
            claimed_steps=['exif', 'ocr'],
        )

    stored = metadata.get_entity(user_id, filename)
    assert stored['exif_status'] == 'done'
    assert stored['ocr_status'] == 'no_data'
