import threading

import pytest

import wave_no_work


class FakeStore:
    def __init__(self, members, existing=None):
        self.members, self.existing, self.commits, self.gets = members, existing or {}, [], 0
        self.library = 'lib'

    def _page(self, prefix, after):
        rows = [m for m in self.members if m['RowKey'] > after]
        return rows[:wave_no_work.MAX_PAGE]

    def _required(self, key):
        return {'RowKey': key}

    def _get(self, key):
        self.gets += 1
        return self.existing.get(key)

    def _row(self, key, **fields):
        return dict(RowKey=key, **fields)

    def _change(self, old, new):
        return ('create' if old is None else 'update', new)

    def _commit(self, control, changes, *, advance=True):
        self.commits.append(list(changes))

    def _retry(self, action):
        return action()


def member(i, outcome='settled'):
    return dict(RowKey=f'm{i:04d}', photoId=f'p{i}', processingRevision='r', evidenceId='e', outcome=outcome)


def test_markers_commit_once_per_page_and_skip_unsettled():
    members = [member(i, 'settled' if i % 5 else 'failed_exhausted') for i in range(45)]
    store, cursors = FakeStore(members), []
    done = wave_no_work.record_publication_members(
        store, {'waveId': 'w'}, lambda: None, checkpoint=cursors.append)
    assert done is True
    assert [len(c) for c in store.commits] == [16, 16, 4]
    assert cursors == ['m0019', 'm0039', 'm0044']


def test_identical_existing_markers_are_not_rewritten():
    members = [member(i) for i in range(3)]
    store = FakeStore(members)
    for m in members:
        key = wave_no_work.publication_key(m)
        store.existing[key] = dict(RowKey=key, waveId='w', photoId=m['photoId'],
                                   processingRevision='r', evidenceId='e')
    wave_no_work.record_publication_members(store, {'waveId': 'w'}, lambda: None)
    assert store.commits == []


def test_map_members_runs_concurrently_and_keeps_context():
    import contextvars
    var = contextvars.ContextVar('v', default=None)
    var.set('parent')
    barrier, seen = threading.Barrier(4, timeout=5), []
    def work(item):
        barrier.wait()
        seen.append(var.get())
    wave_no_work.map_members(range(4), work, 4)
    assert seen == ['parent'] * 4


def test_map_members_raises_earliest_error_after_draining():
    ran = []
    def work(item):
        ran.append(item)
        if item in (1, 2):
            raise ValueError(item)
    with pytest.raises(ValueError) as caught:
        wave_no_work.map_members(range(4), work, 2)
    assert caught.value.args == (1,)


def test_map_members_single_worker_stops_at_first_error():
    ran = []
    def work(item):
        ran.append(item)
        if item == 1:
            raise ValueError
    with pytest.raises(ValueError):
        wave_no_work.map_members(range(4), work, 1)
    assert ran == [0, 1]
