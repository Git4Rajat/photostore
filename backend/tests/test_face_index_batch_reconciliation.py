"""Bounded, fenced batch recovery of filename indexes (never a face cache)."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re

import pytest
from azure.core.exceptions import ResourceNotFoundError

import storage_utils
from test_face_by_filename_lookup import (
    AzureFaceTable,
    FilenameLookupTable,
    _complete_row,
    _face,
    lookup_ctx,
)


_LITERAL = r"'((?:[^']|'')*)'"


class PagedFaceTable(AzureFaceTable):
    """Strict OR/escaped-literal filtering plus Azure-style lazy page failures.

    Page sizes include zero-length pages; failure at len(pages) models a
    continuation request failing instead of reporting successful exhaustion.
    Projection deliberately omits PartitionKey, as the real batch query does.
    """

    def __init__(self):
        super().__init__()
        self.selects = []
        self.page_sizes = None
        self.construction_failure = False
        self.page_failure = None
        self.row_failure = None
        self.pages_started = 0
        self.rows_yielded = 0
        self.exhausted = False
        self.on_exhaustion = None

    def query_entities(self, filter_str, select=None, **kwargs):
        assert kwargs == storage_utils._face_query_request_options()
        self.queries.append(filter_str)
        self.selects.append(select)
        if self.construction_failure:
            raise OSError('query construction outage')
        match = re.fullmatch(r'PartitionKey eq ' + _LITERAL + r'(?: and (.*))?', filter_str)
        assert match is not None, 'Unsupported partition filter'
        user, clause = match.groups()
        user = user.replace("''", "'")
        names = None
        if clause is not None:
            if clause.startswith('('):
                assert clause.endswith(')')
                clause = clause[1:-1]
            name_matches = list(re.finditer(r'filename eq ' + _LITERAL, clause))
            # Validate the entire expression, not just the literals we find.
            assert ' or '.join(m.group(0) for m in name_matches) == clause
            assert name_matches
            names = {m.group(1).replace("''", "'") for m in name_matches}
            assert 1 + len(name_matches) <= 15
        rows = [dict(row) for (partition, _), row in self.rows.items()
                if partition == user and (names is None or row.get('filename') in names)]
        if select is not None:
            rows = [{key: row[key] for key in select if key in row} for row in rows]
        sizes = self.page_sizes if self.page_sizes is not None else [len(rows)]
        assert sum(sizes) == len(rows), 'Explicit page sizes must cover the result'
        pages = []
        offset = 0
        for size in sizes:
            pages.append(rows[offset:offset + size])
            offset += size
        table = self

        class Result:
            def __iter__(self):
                pytest.fail('Batch query must use by_page, not flat iteration')

            def by_page(self):
                def page_rows(index, page):
                    for row_index, row in enumerate(page):
                        if table.row_failure == (index, row_index):
                            raise OSError('row iteration outage')
                        table.rows_yielded += 1
                        yield row

                for index, page in enumerate(pages):
                    if table.page_failure == index:
                        raise OSError('page continuation outage')
                    table.pages_started += 1
                    yield page_rows(index, page)
                if table.page_failure == len(pages):
                    raise OSError('terminal continuation outage')
                table.exhausted = True
                if table.on_exhaustion is not None:
                    table.on_exhaustion()

        return Result()


@pytest.fixture
def batch_ctx(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    assert isinstance(lookup, FilenameLookupTable)
    faces = PagedFaceTable()
    monkeypatch.setitem(storage_utils._CTX, 'face_table_client', faces)

    def no_library_cache(*args, **kwargs):
        pytest.fail('A batch must not read or publish a whole-library face cache')

    monkeypatch.setitem(storage_utils._CTX, 'face_summary_lookup', no_library_cache)
    monkeypatch.setitem(storage_utils._CTX, 'face_summary_cache_writer', no_library_cache)
    return faces, lookup


def _seed(faces, name, face_id, user='u1', **fields):
    faces.upsert_entity({'PartitionKey': user, 'RowKey': face_id,
                         'filename': name, **fields})


def _ids(lookup, name, user='u1'):
    return storage_utils._validated_face_filename_ids(lookup.rows[(user, name)])


def _assert_dirty(lookup, names, user='u1'):
    for name in names:
        row = lookup.rows[(user, name)]
        assert row['state'] == 'dirty'
        assert row['leaseExpiresAt'] == ''
        assert _ids(lookup, name, user) is None
        assert json.loads(row['faceIds']) == []


def _batch_metrics(caplog, names, outcome):
    records = [record for record in caplog.records
               if record.msg == 'face index batch timings metrics=%s']
    assert len(records) == 1
    metrics = json.loads(records[0].args[0])
    assert metrics['outcome'] == outcome
    assert metrics['query_ms'] >= 0 and metrics['total_ms'] >= metrics['query_ms']
    # Check every emitted record, not only the aggregate JSON payload.
    assert all(name not in record.getMessage() for name in names for record in caplog.records)
    assert 'filename' not in metrics and 'faceIds' not in metrics
    return metrics


def test_eight_missing_names_use_one_scan_and_publish_verified_zeros(batch_ctx, caplog):
    faces, lookup = batch_ctx
    names = [f'private-photo-{i}.jpg' for i in range(8)]
    _seed(faces, 'unrelated-secret.jpg', 'unrelated')
    before = deepcopy(faces.rows)
    faces.writes.clear()
    with caplog.at_level('INFO', logger='storage_utils'):
        result = storage_utils.reconcile_face_filename_indexes_batch('u1', names)
    assert len(faces.queries) == 1
    assert faces.selects == [['RowKey', 'filename']]
    assert faces.exhausted
    assert result['requested'] == result['reconciled'] == 8
    assert result['indexed'] == result['ids_retained'] == result['rows_returned'] == 0
    assert result['path'] == 'partition_scan'
    assert set(lookup.rows) == {('u1', name) for name in names}
    assert all(_ids(lookup, name) == [] for name in names)
    assert faces.rows == before and faces.writes == faces.gets == []
    assert _batch_metrics(caplog, names + ['unrelated-secret.jpg'], 'done') == result


def test_legacy_ids_survive_and_normal_writer_point_reads_fresh_curations(batch_ctx):
    faces, lookup = batch_ctx
    name = 'private-legacy.jpg'
    _seed(faces, name, 'rejected', bbox=json.dumps(_face(0)['bbox']),
        rejected=True, reviewStatus='rejected')
    _seed(faces, name, 'confirmed', bbox=json.dumps(_face(100)['bbox']),
        confirmedByUser=True, reviewStatus='confirmed', personId='previous-person')
    lookup.upsert_entity({'PartitionKey': 'u1', 'RowKey': name, 'faceIds': '["stale"]'})
    faces.writes.clear()
    before = deepcopy(faces.rows)
    result = storage_utils.reconcile_face_filename_indexes_batch('u1', [name])
    assert result['ids_retained'] == 2
    assert _ids(lookup, name) == ['confirmed', 'rejected']
    assert faces.writes == [] and faces.rows == before
    # Curation happens AFTER the batch scan: only fresh keyed reads can see it.
    faces.rows[('u1', 'rejected')].update(rejected=True, reviewStatus='rejected')
    faces.rows[('u1', 'confirmed')].update(
        confirmedByUser=True, reviewStatus='confirmed', personId='curated-person',
        assignedByPropagation=True)
    rejected = deepcopy(faces.rows[('u1', 'rejected')])
    faces.queries.clear()
    assert storage_utils._store_client_face_entities('u1', name, [_face(1), _face(101)]) == ['confirmed']
    assert faces.queries == []
    assert set(faces.gets) == {('u1', 'rejected'), ('u1', 'confirmed')}
    assert len(faces.gets) == 2
    assert faces.rows[('u1', 'rejected')] == rejected
    confirmed = faces.rows[('u1', 'confirmed')]
    assert confirmed['personId'] == 'curated-person'
    assert confirmed['confirmedByUser'] and confirmed['assignedByPropagation']
    assert confirmed['reviewStatus'] == 'confirmed'
    assert _ids(lookup, name) == ['confirmed', 'rejected']
    assert set(faces.rows) == {('u1', 'confirmed'), ('u1', 'rejected')}


def test_complete_indexes_are_unchanged_without_any_query(batch_ctx, caplog):
    faces, lookup = batch_ctx
    names = ['private-complete.jpg', 'private-zero.jpg']
    lookup.upsert_entity(_complete_row(['authoritative'], RowKey=names[0]))
    lookup.upsert_entity(_complete_row([], RowKey=names[1]))
    before, versions = deepcopy(lookup.rows), dict(lookup.versions)
    lookup.writes.clear()
    with caplog.at_level('INFO', logger='storage_utils'):
        result = storage_utils.reconcile_face_filename_indexes_batch('u1', names + names)
    assert result['requested'] == result['indexed'] == 2
    assert result['reconciled'] == result['pages'] == result['ids_retained'] == 0
    assert result['path'] == 'indexed'
    assert lookup.rows == before and lookup.versions == versions
    assert lookup.writes == lookup.updates == faces.queries == faces.writes == []
    assert _batch_metrics(caplog, names, 'done') == result


@pytest.mark.parametrize('state', ['dirty', 'expired'])
def test_dirty_or_expired_lookup_is_recovered_from_actual_faces(batch_ctx, state):
    faces, lookup = batch_ctx
    name = 'private-recovery.jpg'
    row = _complete_row(['stale'], RowKey=name, state='dirty')
    if state == 'expired':
        row.update(state='writing', leaseExpiresAt=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    lookup.upsert_entity(row)
    _seed(faces, name, 'actual')
    result = storage_utils.reconcile_face_filename_indexes_batch('u1', [name])
    assert result['reconciled'] == result['ids_retained'] == 1
    assert _ids(lookup, name) == ['actual']
    assert lookup.rows[('u1', name)]['generation'] != row['generation']


def test_active_lease_aborts_partial_acquisition_and_dirties_only_owned_rows(batch_ctx, caplog):
    faces, lookup = batch_ctx
    names = ['private-a.jpg', 'private-b.jpg', 'private-c.jpg', 'private-d.jpg']
    active = _complete_row(['other-writer'], RowKey=names[2], state='writing',
                           leaseExpiresAt=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat())
    lookup.upsert_entity(active)
    _seed(faces, names[0], 'untouched')
    before = deepcopy(faces.rows)
    faces.writes.clear()
    with caplog.at_level('INFO', logger='storage_utils'), pytest.raises(
            storage_utils.FaceFilenameLookupRetryableError, match='active'):
        storage_utils.reconcile_face_filename_indexes_batch('u1', names)
    _assert_dirty(lookup, names[:2])
    assert lookup.rows[('u1', names[2])] == active
    assert ('u1', names[3]) not in lookup.rows
    assert faces.queries == faces.writes == [] and faces.rows == before
    assert _batch_metrics(caplog, names, 'error')['reconciled'] == 0


def test_all_pages_including_empty_pages_exhaust_before_publication(batch_ctx, monkeypatch, caplog):
    faces, lookup = batch_ctx
    names = ['private-paged-a.jpg', 'private-paged-b.jpg']
    _seed(faces, names[0], 'first')
    _seed(faces, names[1], 'last')
    faces.page_sizes = [0, 1, 0, 1, 0]
    original_update = lookup.update_entity

    def only_publish_after_exhaustion(entity, *args, **kwargs):
        if entity['state'] == 'complete':
            assert faces.exhausted and faces.pages_started == 5
        return original_update(entity, *args, **kwargs)

    monkeypatch.setattr(lookup, 'update_entity', only_publish_after_exhaustion)
    with caplog.at_level('INFO', logger='storage_utils'):
        result = storage_utils.reconcile_face_filename_indexes_batch('u1', names)
    assert result['pages'] == 5 and result['rows_returned'] == result['ids_retained'] == 2
    assert _ids(lookup, names[0]) == ['first'] and _ids(lookup, names[1]) == ['last']
    assert _batch_metrics(caplog, names, 'done') == result


@pytest.mark.parametrize('failure', ['construction', 'page', 'row', 'exhaustion', 'finish', 'committed_finish'])
def test_query_or_publication_error_dirties_entire_batch(batch_ctx, monkeypatch, caplog, failure):
    faces, lookup = batch_ctx
    names = ['private-failure-a.jpg', 'private-failure-b.jpg']
    _seed(faces, names[0], 'first')
    _seed(faces, names[1], 'last')
    faces.page_sizes = [1, 1]
    if failure == 'construction':
        faces.construction_failure = True
    elif failure == 'page':
        faces.page_failure = 1
    elif failure == 'row':
        faces.page_sizes = [2]
        faces.row_failure = (0, 1)
    elif failure == 'exhaustion':
        faces.page_failure = 2
    else:
        original_update = lookup.update_entity

        def fail_second_completion(entity, *args, **kwargs):
            if entity['state'] == 'complete' and entity['RowKey'] == names[1]:
                if failure == 'committed_finish':
                    original_update(entity, *args, **kwargs)
                raise OSError('completion outage')
            return original_update(entity, *args, **kwargs)

        monkeypatch.setattr(lookup, 'update_entity', fail_second_completion)
    faces.writes.clear()
    before = deepcopy(faces.rows)
    with caplog.at_level('INFO', logger='storage_utils'), pytest.raises(OSError, match='outage'):
        storage_utils.reconcile_face_filename_indexes_batch('u1', names)
    _assert_dirty(lookup, names)
    assert faces.rows == before and faces.writes == []
    metrics = _batch_metrics(caplog, names, 'error')
    assert metrics['reconciled'] == (1 if failure in {'finish', 'committed_finish'} else 0)
    assert metrics['ids_retained'] == {'construction': 0, 'page': 1, 'row': 1}.get(failure, 2)
    assert metrics['pages'] == {'construction': 0, 'page': 1, 'row': 1}.get(failure, 2)


@pytest.mark.parametrize('when', ['pre', 'mid', 'post', 'publication'])
def test_cancellation_never_publishes_partial_or_false_zero(batch_ctx, caplog, when):
    faces, lookup = batch_ctx
    names = ['private-cancel-a.jpg', 'private-cancel-b.jpg']
    _seed(faces, names[0], 'first')
    _seed(faces, names[1], 'last')
    faces.page_sizes = [1, 0, 1]

    def cancelled():
        if when == 'pre':
            return True
        if when == 'mid':
            return faces.rows_yielded >= 2
        if when == 'post':
            return faces.exhausted
        return any(row['state'] == 'complete' for row in lookup.rows.values())

    faces.writes.clear()
    with caplog.at_level('INFO', logger='storage_utils'), pytest.raises(
            storage_utils.FaceFilenameLookupRetryableError, match='cancelled'):
        storage_utils.reconcile_face_filename_indexes_batch('u1', names, cancelled=cancelled)
    if when == 'pre':
        assert lookup.rows == {} and faces.queries == []
    else:
        _assert_dirty(lookup, names)
        assert len(faces.queries) == 1
    assert faces.writes == []
    metrics = _batch_metrics(caplog, names, 'error')
    assert metrics['ids_retained'] == {'pre': 0, 'mid': 1, 'post': 2, 'publication': 2}[when]
    assert metrics['reconciled'] == (1 if when == 'publication' else 0)


def test_lease_expiring_after_scan_cannot_be_published_as_complete_zero(batch_ctx, caplog):
    faces, lookup = batch_ctx
    names = ['private-expired-a.jpg', 'private-expired-b.jpg']

    def expire_leases():
        for name in names:
            row = dict(lookup.rows[('u1', name)])
            row['leaseExpiresAt'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            lookup.upsert_entity(row)

    faces.on_exhaustion = expire_leases
    with caplog.at_level('INFO', logger='storage_utils'), pytest.raises(
            storage_utils.FaceFilenameLookupRetryableError, match='lease'):
        storage_utils.reconcile_face_filename_indexes_batch('u1', names)
    assert faces.exhausted
    _assert_dirty(lookup, names)
    assert _batch_metrics(caplog, names, 'error')['reconciled'] == 0


@pytest.mark.parametrize('counts, succeeds', [
    ([4096], True), ([4097], False),
    ([4096, 4096], True), ([4096, 4096, 1], False),
])
def test_per_photo_and_batch_id_bounds_fail_closed(batch_ctx, caplog, counts, succeeds):
    faces, lookup = batch_ctx
    names = [f'private-bound-{i}.jpg' for i in range(len(counts))]
    for name, count in zip(names, counts):
        for index in range(count):
            _seed(faces, name, f'{name}-face-{index:04d}')
    faces.writes.clear()
    with caplog.at_level('INFO', logger='storage_utils'):
        if succeeds:
            result = storage_utils.reconcile_face_filename_indexes_batch('u1', names)
            assert result['ids_retained'] == sum(counts)
            assert [len(_ids(lookup, name)) for name in names] == counts
        else:
            with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='bound exceeded'):
                storage_utils.reconcile_face_filename_indexes_batch('u1', names)
            _assert_dirty(lookup, names)
    metrics = _batch_metrics(caplog, names, 'done' if succeeds else 'error')
    assert metrics['ids_retained'] == sum(counts)
    assert metrics['reconciled'] == (len(names) if succeeds else 0)
    assert faces.writes == [] and len(faces.queries) == 1


@pytest.mark.parametrize('names', [[f'private-invalid-{i}.jpg' for i in range(33)], [''], ['   ']])
def test_invalid_input_does_not_touch_storage(batch_ctx, names):
    faces, lookup = batch_ctx
    with pytest.raises(ValueError, match='at most 32 nonempty filenames'):
        storage_utils.reconcile_face_filename_indexes_batch('u1', names)
    assert faces.queries == faces.writes == lookup.gets == lookup.writes == []
    assert lookup.rows == {}


def test_cross_user_rows_and_indexes_are_isolated(batch_ctx):
    faces, lookup = batch_ctx
    name = 'private-shared-name.jpg'
    _seed(faces, name, 'own-face')
    _seed(faces, name, 'foreign-face', user='u2')
    lookup.upsert_entity(_complete_row(['foreign-face'], PartitionKey='u2', RowKey=name))
    foreign = deepcopy(lookup.rows[('u2', name)])
    result = storage_utils.reconcile_face_filename_indexes_batch('u1', [name])
    assert _ids(lookup, name) == ['own-face']
    assert lookup.rows[('u2', name)] == foreign
    assert result['rows_returned'] == result['ids_retained'] == 1
    assert all(user == 'u1' for user, _ in lookup.gets)
    assert "PartitionKey eq 'u1'" in faces.queries[0]


@pytest.mark.parametrize('with_unknown', [False, True])
def test_completion_race_restores_authoritative_ids_instead_of_scanning_them(batch_ctx, monkeypatch, with_unknown):
    faces, lookup = batch_ctx
    name = 'private-raced.jpg'
    names = [name, 'private-unknown.jpg'] if with_unknown else [name]
    _seed(faces, name, 'stale-scan-id')
    faces.writes.clear()
    original_get = lookup.get_entity
    raced = False

    def complete_after_initial_missing_read(partition_key, row_key):
        nonlocal raced
        if row_key == name and not raced:
            raced = True
            lookup.upsert_entity(_complete_row(['authoritative-id'], RowKey=name))
            raise ResourceNotFoundError('Initial snapshot was missing')
        return original_get(partition_key, row_key)

    monkeypatch.setattr(lookup, 'get_entity', complete_after_initial_missing_read)
    result = storage_utils.reconcile_face_filename_indexes_batch('u1', names)
    assert raced and _ids(lookup, name) == ['authoritative-id']
    assert result['indexed'] == 1 and result['reconciled'] == int(with_unknown)
    assert result['ids_retained'] == 0
    if with_unknown:
        assert _ids(lookup, names[1]) == []
        assert len(faces.queries) == 1 and name not in faces.queries[0]
    else:
        assert faces.queries == []
    assert faces.writes == []


@pytest.mark.parametrize('source', ['initial_read', 'acquire_read', 'missing_lookup_client', 'missing_face_client'])
def test_transport_or_unavailable_storage_never_becomes_verified_zero(batch_ctx, monkeypatch, source):
    faces, lookup = batch_ctx
    name = 'private-outage.jpg'
    if source.startswith('missing_'):
        key = 'face_by_filename_table_client' if source == 'missing_lookup_client' else 'face_table_client'
        monkeypatch.setitem(storage_utils._CTX, key, None)
        error = storage_utils.FaceFilenameLookupRetryableError
    else:
        original_get = lookup.get_entity
        calls = 0

        def failed_read(*args, **kwargs):
            nonlocal calls
            calls += 1
            if source == 'initial_read' or calls == 2:
                raise OSError('lookup transport outage')
            return original_get(*args, **kwargs)

        monkeypatch.setattr(lookup, 'get_entity', failed_read)
        error = OSError
    with pytest.raises(error):
        storage_utils.reconcile_face_filename_indexes_batch('u1', [name])
    assert lookup.rows == {} and lookup.writes == []
    assert faces.queries == faces.writes == []


@pytest.mark.parametrize('count', [1, 8, 14, 15, 32])
def test_escaped_or_filters_respect_fifteen_comparisons_and_large_batches_scan_partition(batch_ctx, caplog, count):
    faces, lookup = batch_ctx
    user = "private'user"
    names = [f"private-photo-{i}'s or filename eq 'decoy.jpg" for i in range(count)]
    for index, name in enumerate(names):
        _seed(faces, name, f'wanted-{index}', user=user)
    for index in range(40):
        _seed(faces, f'private-unrelated-{index}.jpg', f'unrelated-{index}', user=user)
    _seed(faces, names[0], 'foreign', user='other-user')
    before = deepcopy(faces.rows)
    faces.writes.clear()
    with caplog.at_level('INFO', logger='storage_utils'):
        result = storage_utils.reconcile_face_filename_indexes_batch(user, names)
    assert len(faces.queries) == 1
    query = faces.queries[0]
    partition_filter = "PartitionKey eq 'private''user'"
    assert query.startswith(partition_filter)
    if count <= 14:
        comparisons = ["filename eq '" + name.replace("'", "''") + "'"
                       for name in sorted(names)]
        expected_clause = comparisons[0] if count == 1 else '(' + ' or '.join(comparisons) + ')'
        assert query == partition_filter + ' and ' + expected_clause
        assert result['rows_returned'] == count
    else:
        assert query == partition_filter
        assert result['rows_returned'] == count + 40
    assert result['ids_retained'] == result['reconciled'] == count
    assert set(lookup.rows) == {(user, name) for name in names}
    assert all(_ids(lookup, name, user) == [f'wanted-{index}']
               for index, name in enumerate(names))
    assert faces.rows == before and faces.writes == faces.gets == []
    assert _batch_metrics(caplog, names, 'done') == result