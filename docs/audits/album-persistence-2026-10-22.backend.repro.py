from types import SimpleNamespace
import app
from tests.fakes import FakeTable


def test_album_mutation_during_legacy_build_has_followup_work(monkeypatch):
    jobs, albums, dirty = FakeTable(), FakeTable(), FakeTable()
    sent = []
    monkeypatch.setattr(app, 'INDEX_MAINTENANCE_QUEUE_NAME', '')
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    monkeypatch.setattr(app, 'albums_table_client', albums)
    monkeypatch.setattr(app, 'albums_dirty_set_table_client', dirty)
    monkeypatch.setattr(app, 'library_ops_queue_client', SimpleNamespace(send_message=lambda *a, **k: sent.append(a)))
    monkeypatch.setattr(app, 'touch_user_albums_index_state', lambda *a: '')
    app.mark_albums_dirty('u1', ['a1'])
    def publish_older_snapshot(*a, **k):
        # The job has captured its dirty-row snapshot. This edit arrives
        # after the index input was read but before that build completes.
        app._save_album_entity({'PartitionKey':'u1','RowKey':'a1','name':'Newer'})
        return []
    monkeypatch.setattr(app, 'prime_all_user_indexes_sequentially', publish_older_snapshot)
    app._run_index_build_job('u1', 'index-build-u1-albums', 'albums')
    assert app.get_dirty_albums('u1') == {'a1'}
    assert sent, 'Newer dirty album work needs a follow-up wake after the active build'
