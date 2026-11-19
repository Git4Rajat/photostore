"""The processing bar's Retry: which stuck waves it re-queues, in what order, and for whom."""
import pytest

import app
import wave_health
import wave_retry
from processing_progress import ProgressStore
from test_processing_progress import wave_row
from test_wave_live_routes import live_api  # noqa: F401  (fixture)
from upload_waves import Conflict


def health(**fields):
    return dict(dict(stage='vision', state='pending', reason='dispatch_pending', ownerActive=False,
                     noProgressSeconds=0), **fields)


@pytest.mark.parametrize('fields,expected', [
    (dict(state='failed', reason='dispatch_exhausted'), True),
    (dict(reason='dispatch_missing'), True),
    (dict(noProgressSeconds=wave_retry.IDLE_RETRY_SECONDS), True),
    (dict(noProgressSeconds=wave_retry.IDLE_RETRY_SECONDS - 1), False),  # just queued or cold-starting
    (dict(state='blocked', blockedByWaveId='older'), False),  # a retry cannot help a waiting wave
    (dict(state='processing', ownerActive=True, noProgressSeconds=9999), False),  # slow but alive
    (dict(stage=None, state='waiting'), False),
])
def test_retryable_only_when_a_retry_can_unblock(fields, expected):
    assert wave_retry.retryable(health(**fields)) is expected


def test_retry_stuck_requeues_unblocked_waves_oldest_first_and_skips_the_rest(monkeypatch):
    waves = [
        dict(waveId='a', createdAt=1, health=health(state='failed', reason='dispatch_exhausted')),
        dict(waveId='b', createdAt=2, health=health(state='blocked')),
        dict(waveId='c', createdAt=3, health=health(state='processing', ownerActive=True)),
        dict(waveId='d', createdAt=4, health=health(reason='dispatch_missing', stage='index')),
        dict(waveId='e', createdAt=5, health=health(state='failed', reason='dispatch_exhausted')),
    ]
    monkeypatch.setattr(wave_retry, 'unfinished', lambda store: waves)
    queued, driven = [], []

    def fake_retry(store, wave_id, current):
        if wave_id == 'e':
            raise Conflict('Stage owner is still active')  # a worker took it after the list
        queued.append(wave_id)
        return current['health']['stage']

    monkeypatch.setattr(wave_retry, 'retry_stage', fake_retry)
    result = wave_retry.retry_stuck(None, driven.append)
    assert [item['waveId'] for item in result['retried']] == ['a', 'd'] == queued == driven
    assert result['retried'][1]['stage'] == 'index'
    assert {item['waveId']: item['reason'] for item in result['skipped']} == {
        'b': 'blocked', 'c': 'working', 'e': 'working'}


def test_unfinished_is_oldest_first_and_leaves_out_open_and_finished_waves(live_api, monkeypatch):  # noqa: F811
    _, store, _, _ = live_api
    for wave_id, state, created in [('w3', 'ready', 30.0), ('w1', 'processing', 10.0), ('w2', 'complete', 20.0),
                                    ('w4', 'collecting', 5.0)]:
        wave_row(store, wave_id, state=state, createdAt=created)
    monkeypatch.setattr(wave_health, 'status', lambda s, w: dict(store._get(store.wave_key(w)), health={}))
    assert [wave['waveId'] for wave in wave_retry.unfinished(store)] == ['w1', 'w3']


def test_the_route_retries_for_the_owner_only(live_api, monkeypatch):  # noqa: F811
    client, _, _, _ = live_api
    called = []
    monkeypatch.setattr(wave_retry, 'retry_stuck', lambda store, drive: called.append(1) or dict(retried=[], skipped=[]))
    body = client.post('/upload/waves/live/retry-stuck', json={}).json
    assert (body['retried'], body['skipped']) == ([], [])
    monkeypatch.setattr(app.library_store, 'is_owner', lambda account, library: False, raising=False)
    assert client.post('/upload/waves/live/retry-stuck', json={}).status_code == 409
    assert called == [1]


def test_the_bar_offers_retry_to_the_owner_and_tells_members_to_ask(live_api, monkeypatch):  # noqa: F811
    client, store, _, _ = live_api
    wave_row(store, 'w1', lastProgressAt=900.0, updatedAt=900.0)
    ProgressStore(store).record_vision('w1', 50, 200, force=True)
    monkeypatch.setattr(wave_health, 'status', lambda s, w: dict(health=health(state='failed', reason='dispatch_exhausted')))
    wave = client.get('/api/processing').json['waves'][0]
    assert wave['problem'] == 'failed' and wave['retryable'] is True and 'askOwner' in wave and not wave['askOwner']
    monkeypatch.setattr(app.library_store, 'is_owner', lambda account, library: False, raising=False)
    wave = client.get('/api/processing').json['waves'][0]
    assert wave['retryable'] is False and wave['askOwner'] is True
    monkeypatch.setattr(wave_health, 'status', lambda s, w: dict(health=health(state='blocked')))
    assert 'retryable' not in client.get('/api/processing').json['waves'][0]


def test_a_failing_relay_does_not_stop_the_other_waves_or_hide_the_retry(monkeypatch):
    waves = [dict(waveId=name, createdAt=order, health=health(state='failed', reason='dispatch_exhausted'))
             for order, name in enumerate('ab')]
    monkeypatch.setattr(wave_retry, 'unfinished', lambda store: waves)
    monkeypatch.setattr(wave_retry, 'retry_stage', lambda store, wave_id, current: 'vision')

    def drive(wave_id):
        if wave_id == 'a':
            raise RuntimeError('No-work proof does not cover sealed intake')
    result = wave_retry.retry_stuck(None, drive)
    assert [item['waveId'] for item in result['retried']] == ['a', 'b']
    assert result['retried'][0]['relayPending'] is True and 'relayPending' not in result['retried'][1]
