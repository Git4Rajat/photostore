"""Vision work is deferred at finalize and released once when the upload ends/goes quiet."""
from __future__ import annotations
from azure.core.exceptions import ResourceNotFoundError

from datetime import datetime, timedelta, timezone

import pytest

import app


def _iso(seconds_ago):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class _Entity(dict):
    def __init__(self, data, etag='e1'):
        super().__init__(data)
        self.metadata = {'etag': etag}


class _Table:
    def __init__(self, activity_row=None):
        self.rows = {}
        self.activity_row = activity_row
        self.updates = []

    def get_entity(self, pk, rk):
        if pk == 'clustering_maintenance':
            if self.activity_row is None:
                raise KeyError(rk)
            return _Entity(self.activity_row)
        raise KeyError(rk)

    def update_entity(self, entity, **kwargs):
        self.updates.append((dict(entity), kwargs))
        if entity.get('PartitionKey') == 'clustering_maintenance' and self.activity_row is not None:
            self.activity_row.update({k: v for k, v in entity.items() if k not in ('PartitionKey', 'RowKey')})


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(app, '_queue_ipwork_processing',
                        lambda user, name, steps=None, **kw: calls.append((name, steps, kw)) or {'status': 'queued'})
    return calls


def test_finalize_defers_instead_of_enqueueing(monkeypatch, queued):
    monkeypatch.setattr(app, 'IPWORK_DEFER_UNTIL_UPLOAD_END', True)
    monkeypatch.setattr(app, 'PROCESSING_MODE', 'both')
    monkeypatch.setattr(app, '_enqueue_processing_steps', lambda *a, **k: None)
    app._queue_upload_processing('u1', 'a.jpg')
    assert queued == []


def test_deferral_off_enqueues_immediately(monkeypatch, queued):
    monkeypatch.setattr(app, 'IPWORK_DEFER_UNTIL_UPLOAD_END', False)
    monkeypatch.setattr(app, 'PROCESSING_MODE', 'both')
    monkeypatch.setattr(app, '_enqueue_processing_steps', lambda *a, **k: None)
    monkeypatch.setattr(app, '_queue_ipwork_processing', lambda user, name: queued.append(name) or {'status': 'queued'})
    app._queue_upload_processing('u1', 'a.jpg')
    assert queued == ['a.jpg']


def test_release_enqueues_pending_once_and_marks_rows(monkeypatch, queued):
    table = _Table()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    rows = [
        {'RowKey': 'a.jpg', 'face_status': ''},
        {'RowKey': 'b.jpg', **{f'{s}_status': 'done' for s in app.IPWORK_STEPS}},
        {'RowKey': 'c.jpg', 'ocr_status': ''},
        {'RowKey': 'd.jpg', 'ipworkEnqueuedAt': _iso(10)},
        {'RowKey': 'clip.mov'},
    ]
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **k: iter(rows))
    stats = app._release_deferred_ipwork('u1')
    assert [name for name, _, _ in queued] == ['a.jpg', 'c.jpg']
    assert all(kw == {'ignore_processing_mode': True} for _, _, kw in queued)
    assert stats['photosQueued'] == 2 and stats['failed'] == 0
    marked = [u['RowKey'] for u, _ in table.updates if 'ipworkEnqueuedAt' in u]
    assert marked == ['a.jpg', 'c.jpg']


def test_release_reoffers_after_enqueue_goes_stale(monkeypatch, queued):
    monkeypatch.setattr(app, 'metadata_table_client', _Table())
    stale = {'RowKey': 'a.jpg', 'ipworkEnqueuedAt': _iso(app.IPWORK_SWEEP_STALE_QUEUED_SECONDS + 5)}
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **k: iter([stale]))
    app._release_deferred_ipwork('u1')
    assert [name for name, _, _ in queued] == ['a.jpg']


def test_claim_only_first_caller_wins_per_upload_activity(monkeypatch):
    table = _Table({'lastUploadActivityAt': _iso(400)})
    monkeypatch.setattr(app, 'metadata_table_client', table)
    assert app._claim_ipwork_release('u1') is True
    assert app._claim_ipwork_release('u1') is False
    table.activity_row['lastUploadActivityAt'] = _iso(0)  # new upload after the release
    assert app._claim_ipwork_release('u1') is True


class _JobsTable:
    def __init__(self):
        self.rows = {}

    def upsert_entity(self, entity, **kwargs):
        key = (entity['PartitionKey'], entity['RowKey'])
        self.rows[key] = {**self.rows.get(key, {}), **entity, 'updatedAt': _iso(0)}

    def get_entity(self, partition_key, row_key):
        try:
            return self.rows[(partition_key, row_key)]
        except KeyError:
            raise ResourceNotFoundError(row_key)


class _Queue:
    def __init__(self):
        self.sent = []

    def send_message(self, body, visibility_timeout=None):
        import json
        self.sent.append((json.loads(body), visibility_timeout))


def test_release_check_is_not_rescheduled_while_one_is_already_fresh(monkeypatch):
    jobs = _JobsTable()
    queue = _Queue()
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    monkeypatch.setattr(app, 'library_ops_queue_client', queue)
    app._queue_ipwork_release_check('u1')
    assert len(queue.sent) == 1
    app._queue_ipwork_release_check('u1')  # a second finalize call in the same upload
    assert len(queue.sent) == 1, 'must not spawn a second independent poll chain'


def test_release_check_uses_its_own_window_not_clusterings(monkeypatch):
    table = _Table({'lastUploadActivityAt': _iso(app.IPWORK_RELEASE_IDLE_SECONDS - 5)})
    queue = _Queue()
    released = []
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, 'jobs_table_client', _JobsTable())
    monkeypatch.setattr(app, 'library_ops_queue_client', queue)
    monkeypatch.setattr(app, 'enqueue_ipwork_release', lambda user, reason: released.append((user, reason)))
    app._run_ipwork_release_check_job('u1', app._ipwork_release_check_job_id('u1'))
    assert released == []
    assert queue.sent and queue.sent[0][1] == int(app.IPWORK_RELEASE_IDLE_SECONDS)  # re-scheduled, not released


def test_release_check_releases_once_truly_quiet(monkeypatch):
    table = _Table({'lastUploadActivityAt': _iso(app.IPWORK_RELEASE_IDLE_SECONDS + 5)})
    released = []
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, 'jobs_table_client', _JobsTable())
    monkeypatch.setattr(app, 'enqueue_ipwork_release', lambda user, reason: released.append((user, reason)))
    app._run_ipwork_release_check_job('u1', app._ipwork_release_check_job_id('u1'))
    assert released == [('u1', 'upload-quiet')]


def test_force_release_ignores_claim_and_recent_enqueue(monkeypatch, queued):
    table = _Table({'lastUploadActivityAt': _iso(400), 'ipworkReleasedAt': _iso(10)})
    monkeypatch.setattr(app, 'metadata_table_client', table)
    recent = {'RowKey': 'a.jpg', 'ipworkEnqueuedAt': _iso(10)}
    monkeypatch.setattr(app, '_iter_metadata_rows_for_user', lambda *a, **k: iter([recent]))
    app._run_ipwork_release_job('u1', 'upload-quiet')
    assert queued == []
    app._run_ipwork_release_job('u1', 'manual', force=True)
    assert [name for name, _, _ in queued] == ['a.jpg']


def test_failed_release_unclaims_so_redelivery_retries(monkeypatch):
    table = _Table({'lastUploadActivityAt': _iso(400)})
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, '_release_deferred_ipwork', lambda user, force=False: {'failed': 1, 'photosQueued': 0, 'stepsQueued': 0})
    with pytest.raises(RuntimeError):
        app._run_ipwork_release_job('u1', 'upload-completed')
    assert table.activity_row['ipworkReleasedAt'] == ''
    assert app._claim_ipwork_release('u1') is True
