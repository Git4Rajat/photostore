"""Coverage for real ETag/optimistic-concurrency on photometadata writes.

Every write path here used to be a blind read-modify-write upsert_entity with
no etag/match_condition -- confirmed live via grep returning zero hits for
either across storage_utils.py, despite _update_metadata_fields already
having retry-loop scaffolding that implied one was intended (it caught
ResourceModifiedError, an exception that can only be raised by a conditional
write, which never happened). Two concurrent writers to the same row (two
ipwork steps, a user edit racing a background step, two lease-claim
attempts) could silently clobber each other. This uses a fake table that
actually simulates etag conflicts (unlike tests/fakes.py's FakeTable, which
just accepts every write) to prove the retry logic really recovers instead
of just not crashing.
"""
from __future__ import annotations

import threading
import json

import pytest
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError

import storage_utils


class _EtagEntity(dict):
    """Minimal stand-in for azure.data.tables.TableEntity: a dict that also
    exposes a `.metadata['etag']` property, which update_entity's real
    match_condition handling reads when no explicit etag= is passed."""

    def __init__(self, data, etag) -> None:
        super().__init__(data)
        self._etag = etag

    @property
    def metadata(self):
        return {'etag': self._etag, 'timestamp': None}


class _OneShotBarrierWrapper:
    """Wraps a threading.Barrier so it only synchronizes its first release --
    a retried caller's second get_entity call must not block waiting for a
    party that will never arrive."""

    def __init__(self, table: '_RacyTable', barrier: threading.Barrier) -> None:
        self._table = table
        self._barrier = barrier
        self._lock = threading.Lock()

    def wait(self, timeout=None):
        result = self._barrier.wait(timeout=timeout)
        with self._lock:
            if self._table.read_barrier is self:
                self._table.read_barrier = None
        return result


class _RacyTable:
    """Fake Table Storage client with real etag-conflict simulation: each row
    carries a version counter bumped on every successful write; update_entity
    raises ResourceModifiedError if the caller's etag doesn't match the
    current one, exactly like the real service's match_condition handling."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self.etags: dict = {}
        self.lock = threading.Lock()
        self.read_barrier = None  # set by a test to synchronize concurrent reads
        self.get_calls = 0
        self.update_calls = 0

    def upsert_entity(self, entity):
        key = (entity['PartitionKey'], entity['RowKey'])
        with self.lock:
            self.etags[key] = self.etags.get(key, 0) + 1
            self.rows[key] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        with self.lock:
            self.get_calls += 1
            if key not in self.rows:
                raise ResourceNotFoundError(str(key))
            data = dict(self.rows[key])
            etag = self.etags.get(key, 0)
        barrier = self.read_barrier
        if barrier is not None:
            barrier.wait(timeout=5)
        return _EtagEntity(data, etag)

    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        key = (entity['PartitionKey'], entity['RowKey'])
        check_etag = etag if etag is not None else entity.metadata.get('etag')
        with self.lock:
            self.update_calls += 1
            if key not in self.rows:
                raise ResourceNotFoundError(str(key))
            current_etag = self.etags.get(key, 0)
            if match_condition is not None and check_etag != current_etag:
                raise ResourceModifiedError('etag mismatch')
            self.etags[key] = current_etag + 1
            self.rows[key] = dict(entity)


@pytest.fixture
def racy_ctx(monkeypatch):
    table = _RacyTable()
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'search_index_dirty_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'blob_service_client', None)
    return table


def _seed(table: _RacyTable, user_id: str, filename: str, **overrides) -> None:
    table.upsert_entity({'PartitionKey': user_id, 'RowKey': filename, 'processing_metadata': '{}', **overrides})


def test_concurrent_update_processing_status_neither_write_is_lost(racy_ctx):
    """Two ipwork steps finishing at the same instant for the same photo must
    both land -- not have one silently overwrite the other's status."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    barrier = threading.Barrier(2)
    table.read_barrier = _OneShotBarrierWrapper(table, barrier)

    def _run(step):
        storage_utils.update_processing_status('u1', 'a.jpg', step, 'done')

    t1 = threading.Thread(target=_run, args=('face',))
    t2 = threading.Thread(target=_run, args=('ocr',))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    final = table.get_entity('u1', 'a.jpg')
    assert final['face_status'] == 'done'
    assert final['ocr_status'] == 'done'
    # Both writes actually landed as two separate versions, not one clobbering
    # the other silently in a single version.
    assert table.etags[('u1', 'a.jpg')] >= 2


def test_concurrent_update_metadata_fields_neither_write_is_lost(racy_ctx):
    """A user rating edit racing a background field update on the same row --
    same invariant as the ipwork-vs-ipwork case above, different call site."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    barrier = threading.Barrier(2)
    table.read_barrier = _OneShotBarrierWrapper(table, barrier)

    def _run(updates):
        storage_utils._update_metadata_fields('u1', 'a.jpg', updates)

    t1 = threading.Thread(target=_run, args=({'rating': 5},))
    t2 = threading.Thread(target=_run, args=({'likes': 3},))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    final = table.get_entity('u1', 'a.jpg')
    assert final['rating'] == 5
    assert final['likes'] == 3


def test_concurrent_lease_claims_the_loser_is_rejected_not_silently_overwritten(racy_ctx):
    """Two workers racing to claim the same unowned lease: exactly one must
    win: the other's retry re-reads the winner's claim and raises, instead of
    both succeeding or the second blindly overwriting the first's ownership."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    barrier = threading.Barrier(2)
    table.read_barrier = _OneShotBarrierWrapper(table, barrier)

    results = {}
    errors = {}

    def _run(owner):
        try:
            results[owner] = storage_utils.claim_processing_lease('u1', 'a.jpg', owner)
        except Exception as exc:
            errors[owner] = exc

    t1 = threading.Thread(target=_run, args=('worker-A',))
    t2 = threading.Thread(target=_run, args=('worker-B',))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    # Exactly one of the two must have won the claim.
    assert len(results) == 1, f'expected exactly one winner, got results={results} errors={errors}'
    assert len(errors) == 1
    winner = next(iter(results))
    assert 'already held by another client' in str(errors[next(iter(errors))])

    final = table.get_entity('u1', 'a.jpg')
    assert final['processing_lease_owner'] == winner


def test_lease_release_rechecks_ownership_against_fresh_read(racy_ctx):
    """A release call for a stale owner_id must not clear a lease that's
    since been (re)claimed by someone else -- even if release's own read
    conflicts with a concurrent reclaim."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg', processing_lease_owner='worker-A', processing_lease_expires_at='2099-01-01T00:00:00+00:00')

    # worker-A releases; meanwhile the row has already moved on to worker-B
    # (simulated directly, no thread needed for this one -- release's own
    # ownership pre-check is what's under test).
    table.upsert_entity({
        'PartitionKey': 'u1', 'RowKey': 'a.jpg', 'processing_metadata': '{}',
        'processing_lease_owner': 'worker-B', 'processing_lease_expires_at': '2099-01-01T00:00:00+00:00',
    })

    storage_utils.release_processing_lease('u1', 'a.jpg', 'worker-A')

    final = table.get_entity('u1', 'a.jpg')
    assert final['processing_lease_owner'] == 'worker-B'  # untouched


def test_batched_processing_status_updates_uses_one_round_trip(racy_ctx):
    """apply_client_processing_results_for_file's single call can finish
    several steps (ocr/face/ai_vision/map_detection/...) for one photo. Each
    update_processing_status call used to be its own full get_entity +
    conditional update_entity round trip; batched_processing_status_updates
    must fold them into exactly one of each, regardless of how many steps
    were reported -- see the 2026-10-01 apply_ms investigation."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    table.get_calls = 0
    table.update_calls = 0

    with storage_utils.batched_processing_status_updates('u1', 'a.jpg'):
        storage_utils.update_processing_status('u1', 'a.jpg', 'ocr', 'done', result={'text': 'hi'})
        storage_utils.update_processing_status('u1', 'a.jpg', 'face', 'done', result={'count': 2})
        storage_utils.update_processing_status('u1', 'a.jpg', 'ai_vision', 'done', result={'tags': ['x']})
        storage_utils.update_processing_status('u1', 'a.jpg', 'map_detection', 'no_data')

    assert table.get_calls == 1
    assert table.update_calls == 1

    final = table.get_entity('u1', 'a.jpg')
    assert final['ocr_status'] == 'done'
    assert final['face_status'] == 'done'
    assert final['ai_vision_status'] == 'done'
    assert final['map_detection_status'] == 'no_data'
    processing = storage_utils._safe_json_load(final['processing_metadata'])
    assert processing['ocr'] == {'text': 'hi'}
    assert processing['face'] == {'count': 2}
    assert processing['ai_vision'] == {'tags': ['x']}
    assert processing['map_detection'] is None


def test_update_processing_status_outside_batch_context_is_unbatched(racy_ctx):
    """No active batch (every caller except apply_client_processing_results_for_file)
    must keep writing immediately, one round trip per call -- unchanged
    behavior for every other update_processing_status caller in the codebase."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')
    table.get_calls = 0
    table.update_calls = 0

    storage_utils.update_processing_status('u1', 'a.jpg', 'ocr', 'done')
    storage_utils.update_processing_status('u1', 'a.jpg', 'face', 'done')

    assert table.get_calls == 2
    assert table.update_calls == 2


def test_batched_processing_status_updates_flushes_partial_batch_on_exception(racy_ctx):
    """If the code inside the batch context raises partway through (e.g. a
    later step's inference blows up), whatever steps were already recorded
    must still be persisted -- matching the old immediate-write behavior,
    where an earlier step's write had already landed by the time a later
    step failed."""
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg')

    with pytest.raises(RuntimeError):
        with storage_utils.batched_processing_status_updates('u1', 'a.jpg'):
            storage_utils.update_processing_status('u1', 'a.jpg', 'ocr', 'done')
            raise RuntimeError('boom')

    final = table.get_entity('u1', 'a.jpg')
    assert final['ocr_status'] == 'done'


@pytest.fixture
def worker_commit_ctx(racy_ctx, monkeypatch):
    table = racy_ctx
    _seed(table, 'u1', 'a.jpg', ocr_status='running', map_detection_status='running',
          processing_lease_owner='worker-A', processing_lease='worker-A',
          processing_lease_expires_at='2099-01-01T00:00:00+00:00', rating=1)
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: None)
    monkeypatch.setattr(storage_utils, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'touch_user_sort_index_dirty', lambda *a, **k: None)
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *a: pytest.fail('Unexpected download'))
    table.get_calls = table.update_calls = 0
    return table


def _apply_worker(**kwargs):
    kwargs.setdefault('client_processing', {'ocr': {'hasData': True, 'text': 'hello'},
                                           'map_detection': {'hasData': False}})
    return storage_utils.apply_client_processing_results_for_file(
        'u1', 'a.jpg', origin='ipworker', lease_owner='worker-A',
        **kwargs)


def test_worker_results_statuses_and_release_use_one_read_one_conditional_write(worker_commit_ctx, caplog):
    table = worker_commit_ctx
    with caplog.at_level('INFO', logger='processing_diagnostics'):
        result = _apply_worker(client_processing={
            'ocr': {'hasData': True, 'text': 'hello'},
            'exif': {'hasData': True, 'data': {'Make': 'Test'}},
        })
    assert table.get_calls == table.update_calls == 1
    stored = table.rows[('u1', 'a.jpg')]
    assert stored['ocrText'] == 'hello'
    assert stored['ocr_status'] == 'done'
    assert stored['map_detection_status'] == 'no_data'
    assert json.loads(stored['processing_metadata'])['client_ocr']['source'] == 'ipworker'
    assert all(stored[key] == '' for key in (
        'processing_lease_owner', 'processing_lease', 'processing_lease_expires_at'))
    assert dict(result) == stored
    assert table.etags[('u1', 'a.jpg')] == 2  # seed + one commit, no blind aggregate upsert
    event = next(json.loads(record.getMessage().removeprefix('processing telemetry '))
                 for record in caplog.records if 'vision_metadata_commit' in record.getMessage())
    assert event['write_attempts'] == 1 and event['retry_reads'] == 0
    assert event['status_updates'] == 1 and event['outcome'] == 'success'
    assert 'a.jpg' not in json.dumps(event) and 'hello' not in json.dumps(event)


def test_worker_commit_retries_only_merge_preserving_concurrent_edits(worker_commit_ctx, monkeypatch):
    table = worker_commit_ctx
    work_calls = []
    def concurrent_edit(*_):
        work_calls.append(True)
        current = dict(table.rows[('u1', 'a.jpg')])
        current.update(rating=5, face_status='done', processing_metadata='{"other":{"kept":true}}')
        table.upsert_entity(current)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', concurrent_edit)
    result = _apply_worker()
    assert work_calls == [True]
    assert table.get_calls == table.update_calls == 2
    assert result['rating'] == 5 and result['face_status'] == 'done'
    processing = json.loads(result['processing_metadata'])
    assert processing['other'] == {'kept': True}
    assert processing['client_ocr']['source'] == 'ipworker'


@pytest.mark.parametrize('change,expected', [
    ({'processing_state': 'deleted'}, 'deleted'),
    ({'processingIsolated': True}, 'isolated'),
    ({'processing_lease_owner': 'worker-B'}, 'owner changed'),
    (None, 'not found'),
])
def test_worker_commit_does_not_resurrect_or_release_a_successor(worker_commit_ctx, monkeypatch, change, expected):
    table = worker_commit_ctx
    def concurrent_edit(*_):
        if change is None:
            del table.rows[('u1', 'a.jpg')]
        else:
            current = dict(table.rows[('u1', 'a.jpg')])
            current.update(change)
            table.upsert_entity(current)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', concurrent_edit)
    error_type = ResourceNotFoundError if change is None else RuntimeError
    with pytest.raises(error_type):
        _apply_worker()
    if change is None:
        assert ('u1', 'a.jpg') not in table.rows
    else:
        stored = table.rows[('u1', 'a.jpg')]
        assert all(stored[key] == value for key, value in change.items())
        assert 'ocrText' not in stored
        assert stored['processing_lease_owner'] == change.get('processing_lease_owner', 'worker-A')


def test_worker_rejects_stale_owner_before_external_mutations(worker_commit_ctx, monkeypatch):
    table = worker_commit_ctx
    table.rows[('u1', 'a.jpg')]['processing_lease_owner'] = 'worker-B'
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', lambda *a, **k: pytest.fail('Stale apply'))
    with pytest.raises(RuntimeError, match='owner changed'):
        _apply_worker()
    assert table.update_calls == 0


def test_worker_rejects_isolation_before_external_mutations(worker_commit_ctx, monkeypatch):
    table = worker_commit_ctx
    table.rows[('u1', 'a.jpg')]['processingIsolated'] = True
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', lambda *a, **k: pytest.fail('Isolated apply'))
    with pytest.raises(RuntimeError, match='isolated'):
        _apply_worker()
    assert table.update_calls == 0


@pytest.mark.parametrize('change,expected', [
    ({'ocr_status': 'done', 'ocrText': 'first result'}, 'step completed'),
    ({'anonymousImageId': 'replacement'}, 'source changed'),
    ({'fileHash': 'replacement'}, 'source changed'),
    # A real reprocess/reupload would stamp a fresh processingRevision at its
    # own finalize (storage_utils.finalize_uploaded_file) before this worker's
    # in-flight commit lands -- same "source changed during application"
    # fencing as anonymousImageId/fileHash above, now also covering the
    # source-owned processing generation (see docs/indexing-clustering-
    # target-state.md step 2, "processing completion authoritative").
    ({'processingRevision': 'successor-generation'}, 'source changed'),
])
def test_worker_conflict_preserves_completed_step_and_source_replacement(worker_commit_ctx, monkeypatch, change, expected):
    table = worker_commit_ctx
    def concurrent_edit(*_):
        current = dict(table.rows[('u1', 'a.jpg')])
        current.update(change)
        table.upsert_entity(current)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', concurrent_edit)
    with pytest.raises(RuntimeError, match=expected):
        _apply_worker()
    stored = table.rows[('u1', 'a.jpg')]
    assert all(stored[key] == value for key, value in change.items())
    assert stored['processing_lease_owner'] == 'worker-A'


@pytest.mark.parametrize('lost_response', [False, True])
def test_worker_commit_transport_failure_never_reports_success(worker_commit_ctx, monkeypatch, lost_response):
    table = worker_commit_ctx
    original = table.update_entity
    def outage(*a, **k):
        if lost_response:
            original(*a, **k)
        raise OSError('commit response unavailable')
    monkeypatch.setattr(table, 'update_entity', outage)
    with pytest.raises(OSError, match='response unavailable'):
        _apply_worker()
    stored = table.rows[('u1', 'a.jpg')]
    assert stored['processing_lease_owner'] == ('' if lost_response else 'worker-A')
    assert stored.get('ocr_status') == ('done' if lost_response else 'running')


def test_worker_partial_failure_atomically_flushes_completed_steps_and_unsticks_others(worker_commit_ctx, monkeypatch):
    table = worker_commit_ctx
    def fail_apply(*a, **k):
        storage_utils.mark_step_done('u1', 'a.jpg', 'ocr', {'text': 'partial'})
        raise RuntimeError('later operation failed')
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', fail_apply)
    with pytest.raises(RuntimeError, match='later operation failed'):
        _apply_worker(claimed_steps=['ocr', 'map_detection'])
    assert table.get_calls == table.update_calls == 1
    stored = table.rows[('u1', 'a.jpg')]
    assert stored['ocr_status'] == 'done'
    assert json.loads(stored['processing_metadata'])['ocr'] == {'text': 'partial'}
    assert stored['map_detection_status'] == 'failed'
    assert stored['processing_lease_owner'] == ''


def test_worker_partial_failure_cannot_flush_into_successor_lease(worker_commit_ctx, monkeypatch):
    table = worker_commit_ctx
    def fail_apply(*a, **k):
        storage_utils.mark_step_done('u1', 'a.jpg', 'ocr')
        current = dict(table.rows[('u1', 'a.jpg')])
        current['processing_lease_owner'] = 'worker-B'
        table.upsert_entity(current)
        raise RuntimeError('original apply failure')
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', fail_apply)
    with pytest.raises(RuntimeError, match='original apply failure'):
        _apply_worker()
    stored = table.rows[('u1', 'a.jpg')]
    assert stored['ocr_status'] == 'running' and stored['processing_lease_owner'] == 'worker-B'


def test_worker_commit_exhausted_conflicts_raise_without_replaying_work(worker_commit_ctx, monkeypatch):
    table = worker_commit_ctx
    calls = []
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: calls.append(True))
    attempts = []
    def conflict(*a, **k):
        attempts.append(True)
        raise ResourceModifiedError('persistent conflict')
    monkeypatch.setattr(table, 'update_entity', conflict)
    monkeypatch.setattr(storage_utils, '_METADATA_UPDATE_RETRY_BASE_SECONDS', 0)
    with pytest.raises(ResourceModifiedError):
        _apply_worker()
    assert len(attempts) == storage_utils._METADATA_UPDATE_MAX_RETRIES
    assert calls == [True]
    assert table.rows[('u1', 'a.jpg')]['processing_lease_owner'] == 'worker-A'


def test_worker_retry_count_is_applied_once_after_cas_conflict(worker_commit_ctx, monkeypatch):
    table = worker_commit_ctx
    def work(*a, **k):
        storage_utils.update_processing_status('u1', 'a.jpg', 'ocr', 'failed', error='test', increment_retry=True)
        current = dict(table.rows[('u1', 'a.jpg')])
        current['rating'] = 5
        table.upsert_entity(current)
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', work)
    result = _apply_worker()
    assert result['retry_count'] == 1 and result['rating'] == 5


@pytest.mark.parametrize('expected,owner', [('old-generation', 'worker-A'), ('generation', 'worker-B'), ('generation', '')])
def test_fenced_browser_rejects_stale_source_or_owner_before_any_result_effect(worker_commit_ctx, monkeypatch, expected, owner):
    from processing_completion import StaleProcessingResult
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg']['processingRevision'] = 'generation'
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', lambda *a, **k: pytest.fail('stale result reached effects'))
    with pytest.raises(StaleProcessingResult):
        storage_utils.apply_client_processing_results_for_file('u1', 'a.jpg', origin='browser',
            lease_owner=owner, expected_processing_revision=expected)
    assert table.update_calls == 0


def test_fenced_browser_result_uses_conditional_status_and_release_commit(worker_commit_ctx):
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg']['processingRevision'] = 'generation'
    result = storage_utils.apply_client_processing_results_for_file('u1', 'a.jpg', origin='browser',
        lease_owner='worker-A', expected_processing_revision='generation', claimed_steps=['ocr'],
        client_processing={'ocr': {'hasData': True, 'text': 'browser text'}})
    assert result['ocrText'] == 'browser text'
    assert result['ocr_status'] == 'done'
    assert result['processing_lease_owner'] == ''
    assert table.get_calls == table.update_calls == 1


def test_completion_intent_and_terminal_status_share_worker_commit(worker_commit_ctx, monkeypatch):
    monkeypatch.setenv("UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED", "true")
    from processing_completion import STEPS
    table = worker_commit_ctx
    row = table.rows['u1', 'a.jpg']
    row.update(anonymousImageId='photo', processingRevision='generation', processingGeneration='d' * 64,
        faceCount=0, **{step + '_status': 'no_data' for step in STEPS})
    row['ocr_status'] = 'running'
    result = _apply_worker(claimed_steps=['ocr'], client_processing={'ocr': {'hasData': True, 'text': 'text'}})
    assert result['processingCompletionPending'] == result['processingCompletionId']
    assert result['processingCompletionRevision'] == 'generation'
    assert table.update_calls == 1


def test_fenced_browser_cannot_commit_after_lease_expires_during_result_application(worker_commit_ctx, monkeypatch):
    from processing_completion import StaleProcessingResult
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg']['processingRevision'] = 'generation'
    def expire(*_):
        row = dict(table.rows['u1', 'a.jpg'])
        row['processing_lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        table.upsert_entity(row)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', expire)
    with pytest.raises(StaleProcessingResult, match='expired'):
        storage_utils.apply_client_processing_results_for_file('u1', 'a.jpg', origin='browser',
            lease_owner='worker-A', expected_processing_revision='generation', claimed_steps=['ocr'],
            client_processing={'ocr': {'hasData': True, 'text': 'late text'}})
    assert table.rows['u1', 'a.jpg']['ocr_status'] == 'running'
    assert 'ocrText' not in table.rows['u1', 'a.jpg']


def test_expired_same_owner_claim_gets_a_new_attempt_but_live_renewal_keeps_it(worker_commit_ctx):
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg'].update(processingFence='old-attempt',
        processing_lease_expires_at='2000-01-01T00:00:00+00:00')
    claimed = storage_utils.claim_processing_lease('u1', 'a.jpg', 'worker-A', steps=['ocr'])
    assert claimed['processingFence'] and claimed['processingFence'] != 'old-attempt'
    renewed = storage_utils.claim_processing_lease('u1', 'a.jpg', 'worker-A', steps=['ocr'])
    assert renewed['processingFence'] == claimed['processingFence']


def test_same_owner_reclaim_rejects_old_attempt_before_applying_result(worker_commit_ctx, monkeypatch):
    from processing_completion import StaleProcessingResult
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg'].update(processingRevision='revision', processingFence='new-attempt')
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', lambda *a, **k: pytest.fail('stale attempt reached effects'))
    with pytest.raises(StaleProcessingResult, match='token changed'):
        _apply_worker(expected_processing_revision='revision', expected_processing_fence='old-attempt')
    assert table.update_calls == 0


def test_terminal_worker_commit_automatically_delivers_before_ack(worker_commit_ctx, monkeypatch):
    from processing_completion import STEPS
    from test_upload_waves import AtomicTable
    from upload_waves import ShadowWaves
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    inbox_table = AtomicTable(); inbox = ShadowWaves(inbox_table, 'u1')
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg'].update(anonymousImageId='photo', processingRevision='revision',
        processingGeneration='e' * 64, faceCount=0, **{step + '_status': 'no_data' for step in STEPS})
    table.rows['u1', 'a.jpg']['ocr_status'] = 'running'
    def deliver(library, intent):
        # Terminal result and recoverable intent are visible before inbox delivery.
        committed = table.get_entity('u1', 'a.jpg')
        assert committed['ocr_status'] == 'done' and committed['processing_lease_owner'] == ''
        assert committed['processingCompletionPending'] == intent['id']
        return inbox.accept_source_completion(intent)
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', deliver)
    _apply_worker(claimed_steps=['ocr'], client_processing={'ocr': {'hasData': True, 'text': 'text'}})
    assert table.get_entity('u1', 'a.jpg')['processingCompletionPending'] == ''
    assert len(inbox_table.rows) == 1
    assert next(iter(inbox_table.rows.values()))['completionQualified'] is False


def test_old_same_owner_attempt_cannot_release_or_heartbeat_successor(worker_commit_ctx):
    from processing_completion import StaleProcessingResult
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg']['processingFence'] = 'new-attempt'
    storage_utils.release_processing_lease('u1', 'a.jpg', 'worker-A', expected_processing_fence='old-attempt')
    assert table.rows['u1', 'a.jpg']['processing_lease_owner'] == 'worker-A'
    assert table.update_calls == 0
    with pytest.raises(StaleProcessingResult):
        storage_utils.heartbeat_processing_lease('u1', 'a.jpg', 'worker-A', expected_processing_fence='old-attempt')
    assert table.update_calls == 0


def test_fenced_heartbeat_does_not_reanimate_expired_attempt(worker_commit_ctx):
    from processing_completion import StaleProcessingResult
    table = worker_commit_ctx
    table.rows['u1', 'a.jpg'].update(processingFence='attempt', processing_lease_expires_at='2000-01-01T00:00:00+00:00')
    with pytest.raises(StaleProcessingResult):
        storage_utils.heartbeat_processing_lease('u1', 'a.jpg', 'worker-A', expected_processing_fence='attempt')
    assert table.update_calls == 0
