"""Page-level socket limits plus enumeration budgets never bless partial scans."""
import json
from types import SimpleNamespace

import pytest

import storage_utils
from test_face_by_filename_lookup import _face, lookup_ctx


def test_face_query_request_options_disable_nested_retries():
    options = storage_utils._face_query_request_options()
    assert options['connection_timeout'] == 5
    assert options['read_timeout'] == 15
    assert options['timeout'] == 10  # service page budget, NOT overall deadline
    assert all(options[key] == 0 for key in ['retry_total', 'retry_connect', 'retry_read', 'retry_status'])


@pytest.mark.parametrize('expired', [False, True])
def test_empty_continuation_pages_renew_or_abort_before_mutations(lookup_ctx, monkeypatch, expired):
    faces, lookup = lookup_ctx
    now = SimpleNamespace(value=0)
    monkeypatch.setattr(storage_utils, 'time', SimpleNamespace(monotonic=lambda: now.value))
    renew = storage_utils._renew_face_filename_write
    renewals = []

    def track_renew(*args):
        renewals.append(now.value)
        renew(*args)

    class Pages:
        def by_page(self):
            now.value = 31
            yield iter([])
            now.value = 62
            yield iter([])
            if expired:
                # Exhaustion itself is slow even when there are NO rows.
                now.value = 91

    options = []

    def query(*args, **kwargs):
        options.append(kwargs)
        return Pages()

    monkeypatch.setattr(faces, 'query_entities', query)
    monkeypatch.setattr(storage_utils, '_renew_face_filename_write', track_renew)
    if expired:
        with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='deadline'):
            storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
        assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'
        assert faces.writes == []
    else:
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
        assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'complete'
    assert renewals[:2] == [31, 62]
    assert options == [storage_utils._face_query_request_options()]


def test_single_scan_timeout_preserves_rejected_source_and_unknown_lookup(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    rejected = {'PartitionKey': 'u1', 'RowKey': 'curated', 'filename': 'photo.jpg',
                'rejected': True, 'bbox': json.dumps(_face(0)['bbox'])}
    faces.upsert_entity(rejected)
    faces.writes.clear()

    def timeout(*args, **kwargs):
        assert kwargs == storage_utils._face_query_request_options()
        raise TimeoutError('read deadline')

    monkeypatch.setattr(faces, 'query_entities', timeout)
    with pytest.raises(TimeoutError):
        storage_utils._store_client_face_entities('u1', 'photo.jpg', [_face(0)])
    assert faces.rows[('u1', 'curated')] == rejected
    assert faces.writes == []
    assert lookup.rows[('u1', 'photo.jpg')]['state'] == 'dirty'


def test_standalone_batch_budget_never_publishes_timed_out_zeros(lookup_ctx, monkeypatch):
    faces, lookup = lookup_ctx
    now = SimpleNamespace(value=0)
    monkeypatch.setattr(storage_utils, 'time', SimpleNamespace(monotonic=lambda: now.value))

    class Pages:
        def by_page(self):
            yield iter([])
            now.value = 91
            # Slow successful exhaustion is not evidence of a safe zero.

    def query(*args, **kwargs):
        assert kwargs == {'select': ['RowKey', 'filename'], **storage_utils._face_query_request_options()}
        return Pages()

    monkeypatch.setattr(faces, 'query_entities', query)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='cancelled'):
        storage_utils.reconcile_face_filename_indexes_batch('u1', ['a.jpg', 'b.jpg'])
    assert faces.writes == []
    for name in ['a.jpg', 'b.jpg']:
        row = lookup.rows[('u1', name)]
        assert row['state'] == 'dirty'
        assert storage_utils._validated_face_filename_ids(row) is None