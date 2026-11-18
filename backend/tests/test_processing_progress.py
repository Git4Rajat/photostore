"""Estimated background progress: steps, throttling, durability, tenancy, push budget."""
import time

import index_events
import processing_progress as progress
from processing_progress import ProgressStore
from test_wave_live_routes import live_api  # noqa: F401  (fixture)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def wave_row(store, wave_id, state='processing', **fields):
    store.table.create_entity(store._row(store.wave_key(wave_id), waveId=wave_id, state=state,
        createdAt=900.0, stateEnteredAt=950.0, memberCount=200, **fields))


def test_steps_are_tenths_and_capped():
    assert [progress.step_of(done, 200) for done in (0, 19, 20, 100, 199, 200)] == [0, 0, 1, 5, 9, 10]
    assert progress.step_of(5, 0) == 0


def test_eta_is_coarse_and_absent_until_enough_samples(live_api):
    _, store, _, _ = live_api
    clock = Clock()
    pg = ProgressStore(store, clock=clock)
    row, _ = pg.record_vision('w1', 0, 200, force=True)
    assert row['etaSeconds'] == -1
    for done in (20, 40, 60):
        clock.now += 30
        row, _ = pg.record_vision('w1', done, 200)
    assert row['etaSeconds'] in progress.ETA_BUCKETS  # 140 left at ~0.67/s -> 210 s -> 300 bucket
    assert row['etaSeconds'] == 300


def test_write_is_throttled_but_a_new_step_always_lands(live_api):
    _, store, _, _ = live_api
    clock = Clock()
    pg = ProgressStore(store, clock=clock)
    pg.record_vision('w1', 1, 200, force=True)
    clock.now += 1
    row, crossed = pg.record_vision('w1', 2, 200)
    assert not crossed and row['done'] == 1  # within the gap and the same step: no write
    row, crossed = pg.record_vision('w1', 25, 200)
    assert crossed and row['step'] == 1 and store._get('progress!w1')['done'] == 25


def test_small_waves_do_not_push_steps(live_api):
    _, store, _, _ = live_api
    pg = ProgressStore(store, clock=Clock())
    _, crossed = pg.record_vision('w1', 5, 10)
    assert not crossed


def test_active_lists_running_and_finished_until_dismissed_per_user(live_api):
    _, store, _, _ = live_api
    clock = Clock()
    pg = ProgressStore(store, clock=clock)
    wave_row(store, 'w1')
    pg.record_vision('w1', 50, 200, force=True)
    waves = pg.active('alice')
    assert [(w['waveId'], w['stage'], w['done'], w['total']) for w in waves] == [('w1', 'vision', 50, 200)]
    current = store._get(store.wave_key('w1'))
    store.table.submit_transaction([store._change(current, dict(current, state='complete', failedMembers=1))])
    assert pg.active('alice')[0]['stage'] == 'done' and pg.active('alice')[0]['failed'] == 1
    pg.dismiss('w1', 'alice')
    assert pg.active('alice') == []
    assert len(pg.active('bob')) == 1
    clock.now += progress.RETAIN_SECONDS + 1
    assert pg.active('bob') == []


def test_routes_are_library_scoped_and_dismiss_persists(live_api):
    client, store, _, library = live_api
    wave_row(store, 'w1')
    ProgressStore(store).record_vision('w1', 50, 200, force=True)
    body = client.get('/api/processing').json
    assert body['waves'][0]['done'] == 50
    assert client.get('/api/processing').headers['Cache-Control'] == 'no-store'
    library[0] = 'other'
    assert client.get('/api/processing').json == {'waves': []}
    library[0] = 'u'
    current = store._get(store.wave_key('w1'))
    store.table.submit_transaction([store._change(current, dict(current, state='complete'))])
    assert client.post('/api/processing/w1/dismiss', json={}).status_code == 200
    assert client.get('/api/processing').json == {'waves': []}


def test_publish_progress_is_on_by_default_budgeted_and_spaced(monkeypatch):
    sent = []
    monkeypatch.setenv('INDEX_EVENTS_ENDPOINT', 'https://example')
    monkeypatch.setattr(index_events, '_enqueue', lambda key, message: sent.append(message))
    index_events._progress.update(day=None, used=0, last={})
    monkeypatch.setenv('PROCESSING_PROGRESS_PUSH', 'false')
    assert not index_events.publish_progress('lib', 'w', 'vision', 1)
    monkeypatch.delenv('PROCESSING_PROGRESS_PUSH')
    assert index_events.publish_progress('lib', 'w', 'vision', 1)
    assert not index_events.publish_progress('lib', 'w', 'vision', 2)  # inside the 30 s gap
    assert index_events.publish_progress('lib', 'w', 'cluster', force=True)  # stage change skips the gap
    monkeypatch.setattr(index_events, 'PROGRESS_DAILY_BUDGET', 2)
    assert not index_events.publish_progress('lib', 'w', 'index', force=True)  # budget is never skipped
    assert [m['stage'] for m in sent] == ['vision', 'cluster']
    assert sent[0]['type'] == 'processing-progress' and 'etaSeconds' in sent[0]
