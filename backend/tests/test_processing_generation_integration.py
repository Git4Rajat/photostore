"""Forced reprocess (app._enqueue_processing_steps(force=True)) must allocate
a fresh processing generation via storage_utils.supersede_processing_
generation; an ordinary retry/re-run (force=False) must not -- see
processing_generation.py and docs/audits/upload-artifact-receipts-2026-10-23.md.
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


def _seed_row(table: _FakeMetadataTable, user_id: str, filename: str, **overrides) -> None:
    row = {
        'PartitionKey': user_id, 'RowKey': filename, 'processing_metadata': '{}',
        'fileHash': 'a' * 64, 'processingRevision': 'rev-1',
        **{step + '_status': 'pending' for step in storage_utils.BROWSER_PROCESSING_STEPS},
        **overrides,
    }
    from processing_generation import allocate_generation
    row.update(allocate_generation(row['processingRevision'], photo_id=filename,
                                   file_hash=row['fileHash'], is_video=False))
    table.upsert_entity(row)


@pytest.fixture
def metadata(monkeypatch):
    table = _FakeMetadataTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', object())
    monkeypatch.setattr(app, 'metadata_table_client', table)
    yield table


def test_supersede_allocates_a_fresh_revision_and_generation(metadata):
    _seed_row(metadata, 'lib-A', 'photo.jpg', face_status='failed', preview_status='done')
    original = dict(metadata.rows[('lib-A', 'photo.jpg')])
    updated = storage_utils.supersede_processing_generation('lib-A', 'photo.jpg')
    assert updated['processingRevision'] != original['processingRevision']
    assert all(updated[step + '_status'] == 'pending' for step in storage_utils.BROWSER_PROCESSING_STEPS)
    assert updated['processingFence'] == '' and updated['processing_lease_owner'] == ''


def test_supersede_raises_for_a_deleted_photo(metadata):
    _seed_row(metadata, 'lib-A', 'photo.jpg', processing_state='deleted')
    with pytest.raises(storage_utils.PhotoNotFoundError):
        storage_utils.supersede_processing_generation('lib-A', 'photo.jpg')


def test_supersede_raises_for_a_missing_photo(metadata):
    with pytest.raises(storage_utils.PhotoNotFoundError):
        storage_utils.supersede_processing_generation('lib-A', 'missing.jpg')


def test_forced_full_reprocess_allocates_a_new_generation_when_enforcement_is_on(metadata, monkeypatch):
    monkeypatch.setenv('PROCESSING_GENERATION_ENFORCEMENT_ENABLED', 'true')
    _seed_row(metadata, 'lib-A', 'photo.jpg', **{step + '_status': 'done' for step in storage_utils.BROWSER_PROCESSING_STEPS})
    monkeypatch.setattr(app, '_get_metadata_entity',
                        lambda uid, name: metadata.get_entity(uid, name))
    original_generation = metadata.rows[('lib-A', 'photo.jpg')]['processingGeneration']
    original_revision = metadata.rows[('lib-A', 'photo.jpg')]['processingRevision']
    results = app._enqueue_processing_steps('lib-A', 'photo.jpg', list(storage_utils.BROWSER_PROCESSING_STEPS), force=True)
    assert all(result['status'] == 'queued' for result in results.values())
    stored = metadata.rows[('lib-A', 'photo.jpg')]
    assert stored['processingRevision'] != original_revision
    # Same live contract/photo/source -- the generation ID is unchanged,
    # confirming this is a real reallocation (new revision), not a no-op.
    assert stored['processingGeneration'] == original_generation


def test_forced_partial_reprocess_never_resets_or_strands_unrelated_steps(metadata, monkeypatch):
    """Enforced partial force is rejected without touching old status/proof.
    Full reprocess remains available; disabled-mode legacy behavior remains.
    """
    monkeypatch.setenv('PROCESSING_GENERATION_ENFORCEMENT_ENABLED', 'true')
    _seed_row(metadata, 'lib-A', 'photo.jpg', **{step + '_status': 'done' for step in storage_utils.BROWSER_PROCESSING_STEPS})
    monkeypatch.setattr(app, '_get_metadata_entity',
                        lambda uid, name: metadata.get_entity(uid, name))
    original = dict(metadata.rows[('lib-A', 'photo.jpg')])
    results = app._enqueue_processing_steps('lib-A', 'photo.jpg', ['face'], force=True)
    assert results['face']['reason'] == 'partial_force_requires_full_reprocess'
    stored = metadata.rows[('lib-A', 'photo.jpg')]
    assert stored['processingRevision'] == original['processingRevision']
    assert stored['processingGeneration'] == original['processingGeneration']
    assert stored['face_status'] == 'done'
    # Every OTHER required step must remain exactly as it was -- 'done',
    # not reset to 'pending' with no queued work behind it.
    untouched = set(storage_utils.BROWSER_PROCESSING_STEPS) - {'face'}
    assert all(stored[step + '_status'] == 'done' for step in untouched)


def test_forced_full_reprocess_is_a_noop_while_enforcement_is_disabled(metadata, monkeypatch):
    """Default (enforcement disabled): force=True behaves exactly as it did
    before processing_generation.py existed -- no generation reallocation
    at all, even when every required step is named."""
    monkeypatch.delenv('PROCESSING_GENERATION_ENFORCEMENT_ENABLED', raising=False)
    _seed_row(metadata, 'lib-A', 'photo.jpg', **{step + '_status': 'done' for step in storage_utils.BROWSER_PROCESSING_STEPS})
    monkeypatch.setattr(app, '_get_metadata_entity',
                        lambda uid, name: metadata.get_entity(uid, name))
    original = dict(metadata.rows[('lib-A', 'photo.jpg')])
    results = app._enqueue_processing_steps('lib-A', 'photo.jpg', list(storage_utils.BROWSER_PROCESSING_STEPS), force=True)
    assert all(result['status'] == 'queued' for result in results.values())
    stored = metadata.rows[('lib-A', 'photo.jpg')]
    assert stored['processingRevision'] == original['processingRevision']
    assert stored['processingGeneration'] == original['processingGeneration']


def test_ordinary_retry_never_allocates_a_new_generation_or_revision(metadata, monkeypatch):
    _seed_row(metadata, 'lib-A', 'photo.jpg', face_status='failed')
    monkeypatch.setattr(app, '_get_metadata_entity',
                        lambda uid, name: metadata.get_entity(uid, name))
    original = dict(metadata.rows[('lib-A', 'photo.jpg')])
    results = app._enqueue_processing_steps('lib-A', 'photo.jpg', ['face'], force=False)
    assert results['face']['status'] == 'queued'
    stored = metadata.rows[('lib-A', 'photo.jpg')]
    assert stored['processingRevision'] == original['processingRevision']
    assert stored['processingGeneration'] == original['processingGeneration']


def test_forced_reenqueue_on_a_missing_photo_reports_error_without_crashing(metadata, monkeypatch):
    monkeypatch.setattr(app, '_get_metadata_entity', lambda uid, name: None)
    results = app._enqueue_processing_steps('lib-A', 'missing.jpg', ['face'], force=True)
    assert results['face']['status'] == 'error'
