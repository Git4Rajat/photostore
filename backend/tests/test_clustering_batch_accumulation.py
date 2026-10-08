"""Accumulate-until-idle clustering batching.

_poll_clustering_queue_batch_once used to receive exactly one page (<=32,
Azure Queue Storage's real per-call ceiling) and only group ADJACENT
same-user messages within it. These tests cover the replacement:
_accumulate_clustering_batch pools messages across possibly many
receive_messages calls until a target size or an idle timeout is hit, and
the grouping that follows spans the WHOLE accumulated pool (not just
adjacent positions), chunking any single user's group back down to the
<=256-unique-face cap FaissAssigner.batch() itself enforces.
"""
from contextlib import contextmanager
import json
import time
from types import SimpleNamespace

import app
import pytest


class PagedQueue:
    """Returns one page per receive_messages call from a fixed script,
    then empty pages forever once the script is exhausted."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.received = []
        self.deleted = []

    def receive_messages(self, **kwargs):
        self.received.append(kwargs)
        if self._pages:
            return self._pages.pop(0)
        return []

    def update_message(self, message, **kwargs):
        return message

    def delete_message(self, message):
        self.deleted.append(message)


def message(filename, user='u', **kwargs):
    return SimpleNamespace(content=json.dumps(dict(type='people_incremental_assign',
        user_id=user, filename=filename, **kwargs)), dequeue_count=1, insertion_time=None)


class FakeAssigner:
    """Lightweight stand-in for FaissAssigner -- records what batch() was
    called with (user, ids, staged) instead of touching real tables, so
    these tests isolate the accumulation/grouping/chunking logic from the
    real staged-write path (already covered by test_clustering_microbatch.py's
    real_staged fixture)."""

    config = SimpleNamespace(io_concurrency=4, coalesce_writes=True)

    def __init__(self):
        self.leases = []
        self.assign_calls = []

    def assign(self, user, filename, face_ids):
        # A group of exactly one message never goes through batch() --
        # _poll_clustering_queue_batch_once routes singleton groups through
        # the regular per-message dispatch path, which calls assign()
        # directly.
        self.assign_calls.append((user, filename, list(face_ids)))
        return {}, set()

    @contextmanager
    def batch(self, user, ids, *, staged=False):
        ids = list(ids)
        self.leases.append((user, ids, staged))
        calls = []

        def assign(filename, faces):
            calls.append((filename, faces))

        assign.metadata_errors = {}
        assign.project = lambda filename: None
        yield assign


@pytest.fixture
def setup(monkeypatch):
    assigner = FakeAssigner()
    monkeypatch.setattr(app, 'PEOPLE_ASSIGNMENT_ENGINE', 'faiss')
    monkeypatch.setattr(app, '_get_live_faiss_assigner', lambda: assigner)
    monkeypatch.setattr(app, '_people_features_available', lambda: True)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *a: {'face_status': 'done'})
    monkeypatch.setattr(app, '_face_ids_awaiting_person_assignment', lambda u, f: [f])
    return assigner


def test_accumulation_stops_at_target_size(setup):
    # 3 pages of 2 messages each; target_size=5 should stop as soon as the
    # pool crosses it (after the 3rd page gives 6), not keep polling.
    pages = [[message(f'{n}.jpg') for n in range(2)],
              [message(f'{n}.jpg') for n in range(2, 4)],
              [message(f'{n}.jpg') for n in range(4, 6)],
              [message('never-reached.jpg')]]
    queue = PagedQueue(pages)
    assert app._poll_clustering_queue_batch_once(
        queue, 'q', 5, batch_size=2, target_size=5, idle_timeout_seconds=60, poll_seconds=0.01)
    assert len(queue.received) == 3
    assert len(setup.leases) == 1
    assert len(setup.leases[0][1]) == 6


def test_accumulation_stops_at_idle_timeout(setup):
    # One page arrives, then nothing -- accumulation must give up and
    # process the single message instead of waiting the full idle window.
    queue = PagedQueue([[message('a.jpg')]])
    started = time.monotonic()
    assert app._poll_clustering_queue_batch_once(
        queue, 'q', 5, batch_size=8, target_size=500, idle_timeout_seconds=0.1, poll_seconds=0.02)
    elapsed = time.monotonic() - started
    assert elapsed < 2
    # A single message is a group of one -- processed via the regular
    # per-message path (assign()), not batch().
    assert setup.assign_calls == [('u', 'a.jpg', ['a.jpg'])]


def test_grouping_spans_messages_across_multiple_pages(setup):
    # Same user's messages land on two separate pages, with a different
    # user's message sandwiched between them on the first page -- the old
    # adjacent-only scan would never have grouped 'u' together across that.
    pages = [[message('a.jpg', user='u'), message('x.jpg', user='other')],
              [message('b.jpg', user='u')]]
    queue = PagedQueue(pages)
    assert app._poll_clustering_queue_batch_once(
        queue, 'q', 5, batch_size=2, target_size=3, idle_timeout_seconds=60, poll_seconds=0.01)
    by_user = {user: ids for user, ids, _staged in setup.leases}
    assert sorted(by_user['u']) == ['a.jpg', 'b.jpg']
    # 'other' has only one message in the whole accumulated pool -- a
    # singleton group, so it goes through assign() directly, not batch().
    assert setup.assign_calls == [('other', 'x.jpg', ['x.jpg'])]


def test_oversized_user_group_is_chunked_into_multiple_staged_batches(setup):
    # 300 messages for one user, one face id each -- must split into
    # multiple <=256-face staged calls instead of one oversized call.
    messages = [message(f'{n}.jpg') for n in range(300)]
    queue = PagedQueue([messages[:32], messages[32:]])
    assert app._poll_clustering_queue_batch_once(
        queue, 'q', 5, batch_size=32, target_size=300, idle_timeout_seconds=60, poll_seconds=0.01)
    assert len(setup.leases) == 2
    sizes = sorted(len(ids) for _user, ids, staged in setup.leases)
    assert sizes == [44, 256]
    assert all(staged for _user, _ids, staged in setup.leases)
    assert sum(sizes) == 300
    assert queue.deleted == messages
