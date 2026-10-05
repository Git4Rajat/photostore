"""Old finished job rows are swept; in-flight and recent ones never are."""
from datetime import datetime, timedelta, timezone

import app
from tests.fakes import FakeTable


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _row(job_id, status, days_ago, pk='u1'):
    return {'PartitionKey': pk, 'RowKey': job_id, 'jobId': job_id, 'status': status, 'updatedAt': _iso(days_ago)}


class _Jobs(FakeTable):
    def query_entities(self, filter_str, **kw):
        import re
        m = re.match(r"PartitionKey eq '([^']*)' and updatedAt lt '(.*)'$", filter_str)
        assert m, filter_str
        return [dict(v) for (p, _), v in self.rows.items() if p == m.group(1) and v['updatedAt'] < m.group(2)]

    def submit_transaction(self, ops):
        for kind, entity in ops:
            assert kind == 'delete'
            self.rows.pop((entity['PartitionKey'], entity['RowKey']), None)


def test_sweep_deletes_only_old_finished_rows(monkeypatch):
    table = _Jobs()
    for row in (_row('old-done', 'done', 40), _row('old-failed', 'failed', 30), _row('old-running', 'running', 40),
                _row('recent-done', 'done', 1), _row('other-user', 'done', 40, pk='u2')):
        table.upsert_entity(row)
    monkeypatch.setattr(app, 'jobs_table_client', table)
    assert app._sweep_old_job_rows('u1') == 2
    assert {rk for (_, rk) in table.rows} == {'old-running', 'recent-done', 'other-user'}


def test_sweep_is_throttled_per_user(monkeypatch):
    calls = []
    monkeypatch.setattr(app, '_sweep_old_job_rows', lambda uid: calls.append(uid))
    monkeypatch.setattr(app, '_JOB_SWEEP_LAST', {})
    import threading
    started = []
    monkeypatch.setattr(threading, 'Thread', lambda target, args, name, daemon: type('T', (), {'start': lambda self: (started.append(args), target(*args))})())
    app._maybe_sweep_old_job_rows('u1')
    app._maybe_sweep_old_job_rows('u1')
    app._maybe_sweep_old_job_rows('u2')
    assert calls == ['u1', 'u2']
