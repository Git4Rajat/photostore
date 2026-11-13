"""Find-more-faces de-dupes per person: repeat requests for one person reuse
its pass in flight (no pile-up of full face-table scans), while another
person gets a pass of their own. Reclusters keep the per-library guard.
Only a missing queue runs a pass inline on core; a failed send doesn't,
since an inline pass has no job record for the de-dupe to see."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app
from tests.fakes import FakeTable

USER = 'lib-A'


class _Queue:
    def __init__(self):
        self.sent = []

    def send_message(self, body):
        self.sent.append(json.loads(body))


@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setattr(app, 'jobs_table_client', FakeTable())
    q = _Queue()
    monkeypatch.setattr(app, 'clustering_queue_client', q)
    return q


def _job(job_id, status, payload, *, minutes_ago=0, job_type='clustering'):
    app.jobs_table_client.upsert_entity({
        'PartitionKey': USER, 'RowKey': job_id, 'jobId': job_id, 'jobType': job_type, 'status': status,
        'payload': json.dumps(payload),
        'updatedAt': (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
    })


def test_repeat_requests_for_one_person_reuse_its_pass(queue):
    first = app._enqueue_propagate_job(USER, 'pihu')
    again = app._enqueue_propagate_job(USER, 'pihu')
    assert first['status'] == again['status'] == 'queued'
    assert again['jobId'] == first['jobId']
    assert [m['personId'] for m in queue.sent] == ['pihu']


def test_another_person_gets_a_pass_while_the_first_is_running(queue):
    pihu = app._enqueue_propagate_job(USER, 'pihu')
    app._upsert_job_status(pihu['jobId'], USER, 'clustering', 'running')
    tanu = app._enqueue_propagate_job(USER, 'tanu')
    assert tanu['status'] == 'queued' and tanu['jobId'] != pihu['jobId']
    assert [m['personId'] for m in queue.sent] == ['pihu', 'tanu']


@pytest.mark.parametrize('status, minutes_ago', [('done', 0), ('failed', 0), ('running', 60)])
def test_finished_or_stale_passes_do_not_block_the_same_person(queue, status, minutes_ago):
    _job('propagate:lib-A:old', status, {'personId': 'pihu'}, minutes_ago=minutes_ago)
    result = app._enqueue_propagate_job(USER, 'pihu')
    assert result['jobId'] != 'propagate:lib-A:old' and len(queue.sent) == 1


def test_a_recluster_in_flight_no_longer_swallows_find_more_faces(queue):
    _job('recluster:lib-A:1', 'running', {})
    result = app._enqueue_propagate_job(USER, 'tanu')
    assert result['jobId'] != 'recluster:lib-A:1' and [m['personId'] for m in queue.sent] == ['tanu']


def test_recluster_guard_still_sees_any_clustering_job_in_flight(queue):
    # Unchanged: upload-triggered reclusters de-dupe against every clustering job, propagation included.
    pihu = app._enqueue_propagate_job(USER, 'pihu')
    assert app._has_active_clustering_job(USER) == pihu['jobId']


def test_batch_leaves_out_people_already_being_searched(queue):
    pihu = app._enqueue_propagate_job(USER, 'pihu')
    batch = app._enqueue_propagate_batch_job(USER, ['pihu', 'tanu', 'rajat'])
    assert batch['status'] == 'queued' and batch['jobId'] != pihu['jobId']
    assert queue.sent[-1]['personIds'] == ['tanu', 'rajat']
    # Everyone covered now: one more request queues nothing and points at a pass in flight.
    again = app._enqueue_propagate_batch_job(USER, ['tanu', 'pihu'])
    assert again == {'status': 'queued', 'jobId': batch['jobId']} and len(queue.sent) == 2
    assert app._enqueue_propagate_job(USER, 'rajat')['jobId'] == batch['jobId']


class _BrokenQueue:
    def send_message(self, body):
        raise RuntimeError('queue send failed')


@pytest.fixture
def routes(monkeypatch):
    from routes import people
    monkeypatch.setattr(app, 'jobs_table_client', FakeTable())
    persons = FakeTable()
    persons.upsert_entity({'PartitionKey': USER, 'RowKey': 'tanu', 'name': 'Tanu'})
    monkeypatch.setattr(app, 'person_table_client', persons)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: (USER, None))
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    inline = []
    monkeypatch.setattr(app, '_propagate_person_identity',
                        lambda uid, pid, **k: inline.append(pid) or {'autoAssignedCount': 0})
    return people, inline


def _find_faces(people):
    with app.app.test_request_context('/api/persons/tanu/find-faces', method='POST'):
        response = people.find_person_faces('tanu')
    return response if isinstance(response, tuple) else (response, 200)


def test_a_failed_hand_off_answers_with_an_error_instead_of_searching_on_core(routes, monkeypatch):
    people, inline = routes
    monkeypatch.setattr(app, 'clustering_queue_client', _BrokenQueue())
    _, status = _find_faces(people)
    assert status == 503 and inline == []


def test_without_a_queue_find_more_faces_still_runs_inline(routes, monkeypatch):
    people, inline = routes
    monkeypatch.setattr(app, 'clustering_queue_client', None)
    response, status = _find_faces(people)
    assert status == 200 and response.get_json()['status'] == 'done' and inline == ['tanu']


def test_merges_still_succeed_when_the_hand_off_fails_but_search_nothing_on_core(routes, monkeypatch):
    people, inline = routes
    monkeypatch.setattr(app, 'clustering_queue_client', _BrokenQueue())
    monkeypatch.setattr(app, '_merge_persons_core', lambda uid, pid, ids, **k: {'mergeId': 'm1'})
    monkeypatch.setattr(app, '_load_user_face_summary_by_id', lambda uid: {})
    monkeypatch.setattr(app, '_person_is_named', lambda uid, pid: True)
    for name in ('mark_people_dirty', 'touch_user_people_index_state', '_trigger_indexer_rebuild'):
        monkeypatch.setattr(app, name, lambda *a, **k: None)
    with app.app.test_request_context('/api/persons/tanu/merge', method='POST', json={'mergeIds': ['x']}):
        single = people.merge_persons('tanu').get_json()
    with app.app.test_request_context('/api/persons/merge/batch', method='POST',
                                      json={'merges': [{'targetPersonId': 'tanu', 'mergeIds': ['y']}]}):
        batch = people.merge_persons_batch().get_json()
    assert single['success'] and single['propagateJobId'] is None
    assert batch['success'] and batch['propagateJobId'] is None
    assert inline == []
