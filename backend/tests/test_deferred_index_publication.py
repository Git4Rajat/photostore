import json
import threading

import pytest
from azure.core.exceptions import ResourceModifiedError

import storage_utils
from tests.fakes import FakeTable

USER = 'owner'


@pytest.fixture
def published(monkeypatch):
    table = FakeTable()
    pending = json.dumps({'revision': 'r', 'kinds': ['lexical', 'sort']})
    for name in ('a.jpg', 'b.jpg', 'c.jpg'):
        table.upsert_entity(dict(PartitionKey=USER, RowKey=name, indexPending=pending))
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    monkeypatch.setattr(storage_utils, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(storage_utils, 'sort_index_skipped', lambda _u: False)
    calls = {'touch': [], 'sort': [], 'dirty': [], 'flush': [], 'hook': []}
    monkeypatch.setattr(storage_utils, 'touch_user_lexical_index_state', lambda u: calls['touch'].append(u))
    monkeypatch.setattr(storage_utils, 'touch_user_sort_index_state', lambda u, **_: calls['sort'].append(u))
    monkeypatch.setattr(storage_utils, '_mark_search_index_dirty_filenames',
                        lambda u, names, kinds=None: calls['dirty'].append((tuple(sorted(names)), tuple(kinds))))
    monkeypatch.setattr(storage_utils, '_flush_dirty_filename_buffer', lambda u, k: calls['flush'].append(k))
    monkeypatch.setattr(storage_utils, 'METADATA_INDEX_REQUEST_HOOK', lambda u, scope: calls['hook'].append(scope))
    return table, calls


def _publish(table, name):
    return storage_utils.publish_metadata_index_intent(USER, name, dict(table.rows[(USER, name)]))


def test_each_photo_publishes_on_its_own_outside_a_deferral(published):
    table, calls = published
    for name in ('a.jpg', 'b.jpg'):
        assert _publish(table, name)
    assert len(calls['touch']) == 2
    assert len(calls['hook']) == 4
    assert table.rows[(USER, 'a.jpg')]['indexPending'] == ''


def test_deferral_publishes_the_whole_batch_once(published):
    table, calls = published
    with storage_utils.deferred_index_publication(USER):
        for name in ('a.jpg', 'b.jpg', 'c.jpg'):
            assert _publish(table, name)
        assert calls['touch'] == []
        assert all(table.rows[(USER, n)]['indexPending'] for n in ('a.jpg', 'b.jpg', 'c.jpg'))
    assert calls['touch'] == [USER] and calls['sort'] == [USER]
    assert sorted(calls['dirty']) == [(('a.jpg', 'b.jpg', 'c.jpg'), ('lexical',)),
                                      (('a.jpg', 'b.jpg', 'c.jpg'), ('sort',))]
    assert len(calls['hook']) == 2
    assert all(table.rows[(USER, n)]['indexPending'] == '' for n in ('a.jpg', 'b.jpg', 'c.jpg'))


def test_failed_batch_publication_keeps_every_outbox(published, monkeypatch):
    table, calls = published
    monkeypatch.setattr(storage_utils, 'METADATA_INDEX_REQUEST_HOOK', lambda *_: 'unavailable')
    with storage_utils.deferred_index_publication(USER):
        _publish(table, 'a.jpg')
        _publish(table, 'b.jpg')
    assert table.rows[(USER, 'a.jpg')]['indexPending'] and table.rows[(USER, 'b.jpg')]['indexPending']


def test_outbox_edited_during_the_batch_is_not_cleared(published):
    table, _ = published
    with storage_utils.deferred_index_publication(USER):
        _publish(table, 'a.jpg')
        newer = json.dumps({'revision': 'newer', 'kinds': ['lexical']})
        table.upsert_entity(dict(PartitionKey=USER, RowKey='a.jpg', indexPending=newer), mode='merge')
    assert table.rows[(USER, 'a.jpg')]['indexPending'] == newer


def test_other_library_is_not_deferred(published):
    table, calls = published
    table.upsert_entity(dict(PartitionKey='other', RowKey='z.jpg',
                             indexPending=json.dumps({'revision': 'r', 'kinds': ['lexical']})))
    with storage_utils.deferred_index_publication(USER):
        storage_utils.publish_metadata_index_intent('other', 'z.jpg', dict(table.rows[('other', 'z.jpg')]))
        assert calls['touch'] == ['other']


def test_worker_threads_share_the_deferral(published):
    table, calls = published
    with storage_utils.deferred_index_publication(USER):
        threads = [threading.Thread(target=_publish, args=(table, n)) for n in ('a.jpg', 'b.jpg', 'c.jpg')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert calls['touch'] == [USER]


class _Props:
    etag = 'e'


def test_lexical_touch_retries_when_another_writer_changed_the_manifest(monkeypatch):
    import lexical_incremental
    commits = []

    def commit(client, manifest, props, settings):
        commits.append(manifest)
        if len(commits) < 3:
            raise ResourceModifiedError('changed')

    monkeypatch.setattr(storage_utils, '_defer_manifest_touch', lambda _u: False)
    monkeypatch.setattr(storage_utils, '_lexical_index_blob_client', lambda _n: object())
    monkeypatch.setattr(storage_utils, 'invalidate_user_lexical_index_cache', lambda _u: None)
    monkeypatch.setattr(storage_utils.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(lexical_incremental, 'capture_manifest', lambda _c: ({'dirty': True}, _Props()))
    monkeypatch.setattr(lexical_incremental, 'commit_manifest', commit)
    storage_utils.touch_user_lexical_index_state(USER)
    assert len(commits) == 3 and commits[-1]['dirty'] is True


def test_lexical_touch_gives_up_after_its_attempts(monkeypatch):
    import lexical_incremental
    attempts = []

    def commit(*_):
        attempts.append(1)
        raise ResourceModifiedError('changed')

    monkeypatch.setattr(storage_utils, '_defer_manifest_touch', lambda _u: False)
    monkeypatch.setattr(storage_utils, '_lexical_index_blob_client', lambda _n: object())
    monkeypatch.setattr(storage_utils, 'invalidate_user_lexical_index_cache', lambda _u: None)
    monkeypatch.setattr(storage_utils.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(lexical_incremental, 'capture_manifest', lambda _c: ({}, _Props()))
    monkeypatch.setattr(lexical_incremental, 'commit_manifest', commit)
    storage_utils.touch_user_lexical_index_state(USER)
    assert len(attempts) == storage_utils._LEXICAL_TOUCH_ATTEMPTS
