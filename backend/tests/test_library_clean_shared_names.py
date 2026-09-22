"""Regression test for the 2026-09-17 fix to _execute_library_clean.

Live incident: a library_clean job on a ~35k-photo library ran for over an
hour without finishing. Root cause was _is_filename_shared -- called once per
photo -- issuing an *unscoped* `RowKey eq X` Table Storage query with no
PartitionKey, so it scanned the entire multi-tenant metadata table on every
single photo instead of a query the storage service could actually target.
The identical problem was already solved for bulk delete via
_shared_names_in_batch (partition-scoped point queries against the
filename_owners index, run concurrently) -- this just ports that fix to
library_clean instead of re-deriving it, and parallelizes the per-photo and
per-table delete loops that were fully sequential before.

This test pins the *correctness* of that port: a filename still referenced by
another library must survive (blob kept, owner row for the survived owner
untouched), a filename owned only by this library must be fully deleted, and
the sharing decision must come from one batched call rather than a per-photo
one.
"""
from __future__ import annotations

import pytest

import app
from fakes import FakeTable


@pytest.fixture
def metadata_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    return table


@pytest.fixture
def filename_owners_table(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(app, 'filename_owners_table_client', table)
    return table


@pytest.fixture(autouse=True)
def _no_op_side_tables(monkeypatch):
    # _execute_library_clean also sweeps these; keep them out of scope for
    # this test by pointing every other table client at None so the second
    # loop's `if client is None: continue` guard skips them cleanly.
    for name in (
        'face_table_client', 'person_table_client', 'albums_table_client',
        'merge_table_client', 'image_names_table_client', 'hash_index_table_client',
        'jobs_table_client',
    ):
        monkeypatch.setattr(app, name, None)
    monkeypatch.setattr(app, 'invalidate_image_names_cache', lambda library_id: None)
    monkeypatch.setattr(app, '_delete_cover_blobs_for_library', lambda library_id: None)
    monkeypatch.setattr(app, 'delete_user_vector_index_data', lambda library_id: None)
    monkeypatch.setattr(app, 'delete_user_lexical_index_data', lambda library_id: None)
    monkeypatch.setattr(app, 'delete_user_tag_embedding_index_data', lambda library_id: None)
    monkeypatch.setattr(app, '_invalidate_metadata_scan_cache', lambda library_id: None)
    monkeypatch.setattr(app, '_delete_upload_temp_files_for_filename', lambda filename: None)
    monkeypatch.setattr(app, 'delete_image_name_mapping', lambda library_id, anonymous_id: None)


def test_shared_filename_survives_cleanup_while_owned_filename_is_deleted(
    monkeypatch, metadata_table, filename_owners_table,
):
    # 'shared.jpg' is owned by both 'lib_a' (being cleaned) and 'lib_b' (not).
    metadata_table.upsert_entity({'PartitionKey': 'lib_a', 'RowKey': 'shared.jpg'})
    metadata_table.upsert_entity({'PartitionKey': 'lib_b', 'RowKey': 'shared.jpg'})
    filename_owners_table.upsert_entity({'PartitionKey': 'shared.jpg', 'RowKey': 'lib_a'})
    filename_owners_table.upsert_entity({'PartitionKey': 'shared.jpg', 'RowKey': 'lib_b'})

    # 'solo.jpg' is only ever owned by 'lib_a'.
    metadata_table.upsert_entity({'PartitionKey': 'lib_a', 'RowKey': 'solo.jpg'})
    filename_owners_table.upsert_entity({'PartitionKey': 'solo.jpg', 'RowKey': 'lib_a'})

    deleted_blobs = []
    monkeypatch.setattr(
        app, '_delete_photo_blobs_if_present',
        lambda physical_name, extra=None: deleted_blobs.append(physical_name) or [],
    )

    batch_calls = []
    real_shared_names_in_batch = app._shared_names_in_batch

    def _spy_shared_names_in_batch(names_set, user_id):
        batch_calls.append(set(names_set))
        return real_shared_names_in_batch(names_set, user_id)

    monkeypatch.setattr(app, '_shared_names_in_batch', _spy_shared_names_in_batch)

    summary = app._execute_library_clean('lib_a')

    # The sharing check ran once, for every filename at once -- not once per
    # photo (which was the actual live bug: an unscoped full-table scan
    # repeated per photo).
    assert batch_calls == [{'shared.jpg', 'solo.jpg'}]

    # solo.jpg's blob was deleted; shared.jpg's was preserved because lib_b
    # still references it.
    assert deleted_blobs == ['solo.jpg']

    # This library's own filename-owner rows are gone regardless of sharing...
    assert ('shared.jpg', 'lib_a') not in filename_owners_table.rows
    assert ('solo.jpg', 'lib_a') not in filename_owners_table.rows
    # ...but lib_b's ownership row for the still-shared blob is untouched.
    assert ('shared.jpg', 'lib_b') in filename_owners_table.rows

    assert summary['photosDeleted'] == 2
    assert summary['blobsDeleted'] == 1
    assert summary['blobErrors'] == 0

    # The second per-table sweep (metadata_table_client is one of the tables
    # it walks) removes lib_a's own rows for both filenames -- sharing only
    # gates the *blob* delete, not the per-library metadata row.
    assert ('lib_a', 'shared.jpg') not in metadata_table.rows
    assert ('lib_a', 'solo.jpg') not in metadata_table.rows
    # lib_b's row for the shared file is a different partition and must
    # survive untouched.
    assert ('lib_b', 'shared.jpg') in metadata_table.rows


def test_stale_job_history_cleared_but_own_library_clean_rows_kept(monkeypatch, metadata_table):
    # Leftover ipwork/clustering job-history rows for lib_a's now-deleted
    # photos should be swept along with everything else. library_clean's own
    # job rows (including this run's, still 'running' until the caller writes
    # 'done' after this function returns) must survive -- they're the audit
    # trail, not stale photo-processing noise.
    jobs_table = FakeTable()
    monkeypatch.setattr(app, 'jobs_table_client', jobs_table)
    jobs_table.upsert_entity({'PartitionKey': 'lib_a', 'RowKey': 'ipwork:lib_a:1', 'jobType': 'ipwork'})
    jobs_table.upsert_entity({'PartitionKey': 'lib_a', 'RowKey': 'cluster:lib_a:1', 'jobType': 'clustering'})
    jobs_table.upsert_entity({'PartitionKey': 'lib_a', 'RowKey': 'libclean:lib_a:current', 'jobType': 'library_clean'})
    jobs_table.upsert_entity({'PartitionKey': 'lib_b', 'RowKey': 'ipwork:lib_b:1', 'jobType': 'ipwork'})

    app._execute_library_clean('lib_a')

    assert ('lib_a', 'ipwork:lib_a:1') not in jobs_table.rows
    assert ('lib_a', 'cluster:lib_a:1') not in jobs_table.rows
    assert ('lib_a', 'libclean:lib_a:current') in jobs_table.rows
    # A different library's job history is a different partition and must
    # survive untouched.
    assert ('lib_b', 'ipwork:lib_b:1') in jobs_table.rows
