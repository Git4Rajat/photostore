"""A drained page's rows are reread side by side, not member by member, with the same outcome."""
import threading

import pytest
from azure.core.exceptions import ResourceNotFoundError

from upload_wave_execution import _FreshRows
from tests.test_wave_source_batch import world, admit, run, publish_completion  # noqa: F401


class Table:
    def __init__(self, rows):
        self.rows, self.reads = rows, []

    def get_entity(self, library, filename):
        self.reads.append((library, filename, threading.current_thread().name))
        if filename not in self.rows:
            raise ResourceNotFoundError('missing')
        return dict(self.rows[filename])


def settled(rows, names, workers=4):
    fresh = _FreshRows(Table(rows), 'lib', names, workers)
    for future in fresh.futures.values():
        future.exception()
    return fresh


def test_each_row_is_read_once_and_served_to_the_two_settlement_reads():
    fresh = settled({'a': {'v': 1}}, ['a', 'a'])
    assert fresh.get_entity('lib', 'a') == {'v': 1}
    assert fresh.get_entity('lib', 'a') == {'v': 1}
    assert len(fresh.table.reads) == 1
    fresh.table.rows['a']['v'] = 2
    assert fresh.get_entity('lib', 'a') == {'v': 2}  # Later reads are fresh.
    assert len(fresh.table.reads) == 2


def test_served_rows_are_copies():
    fresh = settled({'a': {'v': 1}}, ['a'])
    fresh.get_entity('lib', 'a')['v'] = 9
    assert fresh.get_entity('lib', 'a') == {'v': 1}


def test_absent_row_raises_not_found():
    fresh = settled({}, ['gone'])
    with pytest.raises(ResourceNotFoundError):
        fresh.get_entity('lib', 'gone')


def test_failed_read_falls_through_to_the_table():
    table = Table({'a': {'v': 1}})
    original, calls = table.get_entity, []
    def flaky(library, filename):
        calls.append(filename)
        if len(calls) == 1:
            raise RuntimeError('boom')
        return original(library, filename)
    table.get_entity = flaky
    fresh = _FreshRows(table, 'lib', ['a'], 1)
    fresh.futures['a'].exception()
    assert fresh.get_entity('lib', 'a') == {'v': 1}
    assert len(calls) == 2


def test_other_libraries_and_unlisted_rows_go_to_the_table():
    fresh = settled({'a': {'v': 1}, 'b': {'v': 2}}, ['a'])
    assert fresh.get_entity('lib', 'b') == {'v': 2}
    assert fresh.get_entity('other', 'a') == {'v': 1}
    assert len(fresh.table.reads) == 3


def outcomes(world, monkeypatch, parallel, count=12):
    monkeypatch.setenv('IPWORK_VISION_SETTLEMENT_PARALLEL', parallel)
    metadata, wave, key = admit(world, count)
    store = world[0]
    def process(library, filename, revision, renew):
        index = int(filename.split('-')[1].split('.')[0])
        if index % 4 == 1:
            raise RuntimeError('source failed')
        if index % 4 == 2:
            return  # No completion published.
        publish_completion(store, metadata, filename=filename)
    result = run(world, metadata, wave, key, process, source_workers=3)
    rows = sorted((member['filename'], member['outcome']) for member in store._page(f'e!{wave}!m!'))
    return result['state'], rows


@pytest.mark.parametrize('parallel', ['true', 'false'])
def test_outcomes_match_with_and_without_parallel_reads(world, monkeypatch, parallel):
    state, rows = outcomes(world, monkeypatch, parallel)
    assert state == 'ready'
    assert dict(rows) == {f'photo-{i}.jpg': 'settled' if i % 4 in (0, 3) else 'failed_exhausted' for i in range(12)}


def reader_threads(world, monkeypatch, parallel):
    seen = []
    metadata, wave, key = admit(world, 6)
    store = world[0]
    original = metadata.get_entity
    def get(*args):
        seen.append(threading.current_thread().name)
        return original(*args)
    monkeypatch.setattr(metadata, 'get_entity', get)
    monkeypatch.setenv('IPWORK_VISION_SETTLEMENT_PARALLEL', parallel)
    run(world, metadata, wave, key,
        lambda library, filename, revision, renew: publish_completion(store, metadata, filename=filename))
    return [name for name in seen if name.startswith('vision-settle')]


def test_parallel_reads_run_on_settlement_threads(world, monkeypatch):
    assert reader_threads(world, monkeypatch, 'true')


def test_switch_off_reads_without_settlement_threads(world, monkeypatch):
    assert not reader_threads(world, monkeypatch, 'false')
