"""Acceptance scenarios for docs/indexing-clustering-target-state.md step 2
item 5 ("prove the failure cases before enabling release") not already
covered by test_processing_generation.py, test_processing_generation_
integration.py, test_metadata_write_concurrency.py's parametrized source-
changed cases, or test_processing_cleanup.py's unit coverage:

- Legacy duplicates with missing provenance cannot falsely qualify.
- Interrupted cleanup resumes safely.
- Cleanup preserves artifacts needed by another active wave.
- Late workers cannot republish deleted data.
"""
import json

import pytest
from azure.core.exceptions import ResourceNotFoundError

from fakes import FakeTable
from processing_completion import prepare_completion
from processing_cleanup import delete_orphaned_manifest, orphaned_manifest_candidates, referenced_manifest_ids
from processing_fence import ResultFence, result_fence
from processing_generation import allocate_generation, is_superseded
from upload_waves import digest
from test_processing_fence import world  # noqa: F401 -- shared fixture


def test_legacy_duplicate_with_missing_provenance_cannot_falsely_qualify():
    """A photo whose row was written by a legacy path that never allocated a
    generation (or a dedup/duplicate reference copied fields without one)
    must read as superseded/ineligible, never as an already-current,
    qualified generation by omission."""
    legacy_row = dict(RowKey='legacy.jpg', anonymousImageId='photo-legacy', fileHash='c' * 64,
                      processingRevision='old-rev', face_status='done', faceCount=0)
    assert is_superseded(legacy_row, is_video=False) is True
    assert prepare_completion(legacy_row) is None
    assert 'processingCompletionOutbox' not in legacy_row


def test_interrupted_cleanup_resumes_safely():
    """A crash between deleting the manifest's blobs and deleting the
    manifest row itself must not corrupt a retry: the retry sees the
    already-gone blob/snapshot as tolerable no-ops and still removes the
    manifest row on the second attempt."""
    table = FakeTable()
    partition = 'processing-artifacts-' + digest('lib')
    table.upsert_entity(dict(PartitionKey=partition, RowKey='face!snap-1', payload='{}'))
    row = dict(PartitionKey=partition, RowKey='manifest!orphaned',
              payload=json.dumps(dict(derivatives={}, faceSnapshots=['snap-1'])))
    table.upsert_entity(row)

    class _NoBlobStorage:
        _CTX = {}  # No thumbnail container configured -- blob step is skipped.

    first = delete_orphaned_manifest(table, _NoBlobStorage(), row)
    assert first == dict(deletedBlobs=0, deletedSnapshots=1, skippedBlobs=0, skippedSnapshots=0)
    assert (partition, 'manifest!orphaned') not in table.rows

    # Simulate a crash recovery: the caller re-derives the same candidate
    # (e.g. from a re-run of orphaned_manifest_candidates) and retries.
    # Re-seed only the manifest row (as if the first attempt's final delete
    # never landed) -- the snapshot is genuinely already gone this time.
    table.upsert_entity(row)
    second = delete_orphaned_manifest(table, _NoBlobStorage(), row)
    assert second == dict(deletedBlobs=0, deletedSnapshots=0, skippedBlobs=0, skippedSnapshots=0)  # Already gone; tolerated.
    assert (partition, 'manifest!orphaned') not in table.rows


def test_cleanup_preserves_artifacts_needed_by_another_active_wave():
    """Two photos' manifests exist; one is still referenced by its row's
    committed artifactReceipt (its wave may still need it), the other has
    no referencing row left. Only the unreferenced one is a candidate."""
    table = FakeTable()
    partition = 'processing-artifacts-' + digest('lib')
    table.upsert_entity(dict(PartitionKey=partition, RowKey='manifest!active-wave', payload='{}'))
    table.upsert_entity(dict(PartitionKey=partition, RowKey='manifest!orphaned', payload='{}'))

    live_rows = [dict(artifactReceipt=json.dumps({'id': 'active-wave'}))]
    referenced = referenced_manifest_ids(live_rows)
    candidates = orphaned_manifest_candidates(table, 'lib', referenced)

    assert [row['RowKey'] for row in candidates] == ['manifest!orphaned']


def test_late_worker_cannot_republish_after_permanent_deletion(world):
    """Once a photo's row is marked deleted (the permanent-deletion path's
    first durable step), a worker still holding an old lease/attempt for it
    must be rejected at the very next fence check -- it cannot publish a
    derivative or any other artifact into a deleted photo's identity."""
    metadata, *_, fence = world
    row = metadata.get_entity('lib', 'photo.jpg')
    row['processing_state'] = 'deleted'
    metadata.upsert_entity(row)
    with result_fence(fence):
        with pytest.raises(Exception):
            fence.check()
