"""Isolated regression tests for streamed library and orphan-shadow cleanup."""
import re

import pytest

import app
from fakes import FakeTable


class StreamingTable(FakeTable):
    def __init__(self):
        super().__init__()
        self.queries = []
        self.delete_calls = []
        self.yielded = 0

    def query_entities(self, query, select=None):
        self.queries.append((query, select))
        match = re.fullmatch(r"(PartitionKey|userId) eq '((?:[^']|'')*)'", query)
        assert match, query
        field, value = match.groups()
        value = value.replace("''", "'")
        # Snapshot fake keys so the generator permits deletes during iteration.
        for key in tuple(self.rows):
            row = self.rows.get(key)
            if row is not None and row.get(field) == value:
                self.yielded += 1
                yield {name: row[name] for name in select if name in row} if select else dict(row)

    def delete_entity(self, partition_key, row_key):
        self.delete_calls.append((partition_key, row_key))
        super().delete_entity(partition_key, row_key)


@pytest.fixture(autouse=True)
def isolated_cleanup(monkeypatch):
    for name in vars(app):
        if name.endswith('_table_client'):
            monkeypatch.setattr(app, name, None)
    monkeypatch.setattr(app, 'blob_service_client', None)
    for name in (
        'invalidate_image_names_cache', '_delete_cover_blobs_for_library',
        'delete_user_vector_index_data', 'delete_user_lexical_index_data',
        'delete_user_tag_embedding_index_data', 'delete_user_sort_index_data',
        'delete_user_access_index_data', 'delete_user_albums_index_data',
        'delete_user_people_index_data', '_invalidate_metadata_scan_cache',
        '_delete_upload_temp_files_for_filename', 'delete_image_name_mapping',
    ):
        monkeypatch.setattr(app, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(app, '_shared_names_in_batch', lambda *args: set())
    monkeypatch.setattr(app, '_delete_photo_blobs_if_present', lambda *args: [])


@pytest.mark.parametrize('cleanup', [app._purge_library_data, app._execute_library_clean])
def test_cleanup_deletes_shadows_and_orphans_before_people(monkeypatch, cleanup):
    library = "lib'a"
    people, members, filenames, metadata = (StreamingTable() for _ in range(4))
    for name, table in (
        ('person_table_client', people), ('person_members_table_client', members),
        ('face_by_filename_table_client', filenames), ('metadata_table_client', metadata),
    ):
        monkeypatch.setattr(app, name, table)
    people.upsert_entity({'PartitionKey': library, 'RowKey': 'p1'})
    people.upsert_entity({'PartitionKey': 'other', 'RowKey': 'p2'})
    for pid, owner in [('p1', library), ('orphan', library), ('p2', 'other')]:
        members.upsert_entity({'PartitionKey': pid, 'RowKey': 'f1', 'userId': owner})
    for owner in (library, 'other'):
        filenames.upsert_entity({'PartitionKey': owner, 'RowKey': 'photo.jpg', 'faceIds': '["f1"]'})
        metadata.upsert_entity({'PartitionKey': owner, 'RowKey': 'photo.jpg'})
    original_delete = people.delete_entity
    def delete_person(partition_key, row_key):
        assert ('p1', 'f1') not in members.rows
        assert ('orphan', 'f1') not in members.rows
        assert (library, 'photo.jpg') not in filenames.rows
        original_delete(partition_key, row_key)
    monkeypatch.setattr(people, 'delete_entity', delete_person)
    cleanup(library)
    assert set(people.rows) == {('other', 'p2')}
    assert set(members.rows) == {('p2', 'f1')}
    assert set(filenames.rows) == {('other', 'photo.jpg')}
    assert (f"userId eq 'lib''a'", ['PartitionKey', 'RowKey']) in members.queries
    assert all(select is not None for _, select in people.queries + members.queries + filenames.queries)
    # Repeated cleanup is idempotent, including the orphan fallback.
    cleanup(library)
    assert set(members.rows) == {('p2', 'f1')}


@pytest.mark.parametrize('cleanup', [app._purge_library_data, app._execute_library_clean])
def test_shadow_failure_retries_and_does_not_delete_people(monkeypatch, cleanup, caplog):
    people, members = StreamingTable(), StreamingTable()
    monkeypatch.setattr(app, 'person_table_client', people)
    monkeypatch.setattr(app, 'person_members_table_client', members)
    people.upsert_entity({'PartitionKey': 'lib', 'RowKey': 'p1'})
    members.upsert_entity({'PartitionKey': 'p1', 'RowKey': 'f1', 'userId': 'lib'})
    attempts = []
    def fail(**kwargs):
        attempts.append(kwargs)
        raise RuntimeError('delete unavailable')
    monkeypatch.setattr(members, 'delete_entity', fail)
    with pytest.raises(RuntimeError, match='delete unavailable'):
        cleanup('lib')
    assert len(attempts) == 3
    assert ('lib', 'p1') in people.rows
    assert 'retry required' in caplog.text


@pytest.mark.parametrize('cleanup', [app._purge_library_data, app._execute_library_clean])
def test_shadow_query_failure_is_not_reported_as_success(monkeypatch, cleanup):
    members = StreamingTable()
    monkeypatch.setattr(app, 'person_members_table_client', members)
    def fail(*args, **kwargs):
        raise RuntimeError('scan unavailable')
    monkeypatch.setattr(members, 'query_entities', fail)
    with pytest.raises(RuntimeError, match='scan unavailable'):
        cleanup('lib')


def test_cleanup_streams_large_metadata_in_bounded_batches(monkeypatch):
    metadata, faces = StreamingTable(), StreamingTable()
    monkeypatch.setattr(app, 'metadata_table_client', metadata)
    monkeypatch.setattr(app, 'face_table_client', faces)
    for i in range(351):
        metadata.upsert_entity({'PartitionKey': 'lib', 'RowKey': f'photo{i}'})
        faces.upsert_entity({'PartitionKey': 'lib', 'RowKey': f'face{i}'})
    batches = []
    cleaned = []
    def shared(names, library):
        assert 0 < len(names) <= 100
        # At most one metadata batch was consumed ahead of completed photo work.
        assert metadata.yielded <= len(cleaned) + 100
        batches.append(len(names))
        return set()
    monkeypatch.setattr(app, '_shared_names_in_batch', shared)
    monkeypatch.setattr(app, '_delete_photo_blobs_if_present', lambda name, extra: cleaned.append(name) or [])
    original = faces.delete_entity
    def delete_face(**kwargs):
        assert faces.yielded <= len(faces.delete_calls) + 2 * app.DELETE_IO_CONCURRENCY
        original(**kwargs)
    monkeypatch.setattr(faces, 'delete_entity', delete_face)
    summary = app._execute_library_clean('lib')
    assert summary == {'photosDeleted': 351, 'blobsDeleted': 351, 'blobErrors': 0}
    assert batches == [100, 100, 100, 51]
    assert metadata.rows == faces.rows == {}


def test_cleanup_transient_shadow_delete_failure_recovers(monkeypatch):
    table = StreamingTable()
    monkeypatch.setattr(app, 'face_by_filename_table_client', table)
    table.upsert_entity({'PartitionKey': 'lib', 'RowKey': 'photo'})
    original = table.delete_entity
    calls = []
    def flaky(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError('transient')
        original(**kwargs)
    monkeypatch.setattr(table, 'delete_entity', flaky)
    app._purge_library_data('lib')
    assert len(calls) == 2
    assert table.rows == {}