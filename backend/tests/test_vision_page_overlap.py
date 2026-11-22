"""The next member page loads while the current page's sources run."""
import json
import threading
import time

import pytest

from execution_lease import OwnershipLost
from upload_wave_execution import _NextPage
from vision_buffer import MemberWindow
from tests.test_wave_source_batch import world, admit, run, publish_completion  # noqa: F401


def track_pages(monkeypatch):
    events = []
    original = MemberWindow.page
    def page(self, after, reloaded=False):
        events.append(('page', threading.current_thread().name))
        return original(self, after, reloaded)
    monkeypatch.setattr(MemberWindow, 'page', page)
    return events


def test_next_page_loads_while_current_page_sources_are_running(world, monkeypatch):
    metadata, wave, key = admit(world, 45)
    store = world[0]
    events = track_pages(monkeypatch)
    calls, lock, overlapped = [], threading.Lock(), []
    def process(library, filename, revision, renew):
        with lock:
            first = not calls
            calls.append(filename)
        if first:
            # Page one's first source cannot finish until page two has loaded.
            deadline = time.monotonic() + 5
            while sum(1 for event in events if event[0] == 'page') < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            overlapped.append(sum(1 for event in events if event[0] == 'page') >= 2)
        publish_completion(store, metadata, filename=filename)
    result = run(world, metadata, wave, key, process, source_workers=2, page_overlap=True)
    assert result['state'] == 'ready'
    assert overlapped == [True], 'next page was not prefetched while sources ran'
    assert len(calls) == len(set(calls)) == 45
    assert events[1][1].startswith('wave-next-page')


def test_overlap_off_loads_pages_on_the_stage_thread(world, monkeypatch):
    metadata, wave, key = admit(world, 45)
    store = world[0]
    events = track_pages(monkeypatch)
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
    result = run(world, metadata, wave, key, process, source_workers=2, page_overlap=False)
    assert result['state'] == 'ready'
    assert len(calls) == len(set(calls)) == 45
    assert not any(name.startswith('wave-next-page') for _, name in events)


def test_environment_switch_disables_overlap(world, monkeypatch):
    monkeypatch.setenv('IPWORK_VISION_PAGE_OVERLAP', 'false')
    metadata, wave, key = admit(world, 45)
    store = world[0]
    events = track_pages(monkeypatch)
    def process(library, filename, revision, renew):
        publish_completion(store, metadata, filename=filename)
    run(world, metadata, wave, key, process, source_workers=2)
    assert not any(name.startswith('wave-next-page') for _, name in events)


def test_deadline_restart_with_prefetched_page_runs_every_source_once(world, monkeypatch):
    metadata, wave, key = admit(world, 45)
    store, _, leases, *_ = world
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    calls = []
    def process(library, filename, revision, renew):
        calls.append(filename)
        publish_completion(store, metadata, filename=filename)
        clock[0] += 1
    run(world, metadata, wave, key, process, deadline=5, page_overlap=True)
    first = len(calls)
    assert 0 < first < 45
    position = json.loads(leases._get(leases.key(store.library, wave, 'vision'))['progressCheckpoint'][7:])
    assert position['done'] == first  # The prefetched page was never counted or checkpointed.
    for _ in range(60):
        if store.status(wave)['state'] == 'ready': break
        run(world, metadata, wave, key, process, deadline=clock[0] + 5, page_overlap=True)
    assert len(calls) == len(set(calls)) == 45
    assert store.status(wave)['state'] == 'ready'


def test_ownership_loss_with_prefetch_in_flight_stops_cleanly(world, monkeypatch):
    metadata, wave, key = admit(world, 45)
    store, _, leases, *_ = world
    track_pages(monkeypatch)
    lock, calls = threading.Lock(), []
    def process(library, filename, revision, renew):
        with lock:
            calls.append(filename)
            first = len(calls) == 1
        if first:
            row = leases._get(leases.key(store.library, wave, 'vision'))
            leases._replace(row, dict(row, owner='replacement-owner'))
            renew()
    with pytest.raises(OwnershipLost):
        run(world, metadata, wave, key, process, source_workers=2, page_overlap=True)
    assert len(calls) < 45
    assert not [t for t in threading.enumerate() if t.name.startswith('wave-next-page')]
    assert leases._get(leases.key(store.library, wave, 'vision'))['owner'] == 'replacement-owner'


class _Window:
    def __init__(self):
        self.keys, self.cleared, self.done = [1], False, False
        self.rows = self
    def clear(self):
        self.cleared = True
    def page(self, after):
        return []


class _Names:
    library = 'lib'


def test_page_loaded_for_another_cursor_is_discarded_and_window_reset():
    window = _Window()
    pages = _NextPage(window, _Names(), object())
    try:
        pages.start('a')
        assert pages.take('b', {'x'}) is None
        assert window.cleared and window.keys == []
        assert pages.take('b', ()) is None  # Nothing pending afterwards.
    finally:
        pages.close()


def test_names_written_by_the_current_page_are_dropped_from_the_next_snapshot():
    class Table:
        pass
    window = _Window()
    pages = _NextPage(window, _Names(), Table())
    try:
        pages.start('a')
        members, done, metadata = pages.take('a', {'same.jpg'})
        metadata.rows['same.jpg'] = {'RowKey': 'same.jpg'}
        pages.start('a')
        metadata = pages.take('a', {'same.jpg'})[2]
        assert 'same.jpg' not in metadata.rows
    finally:
        pages.close()
