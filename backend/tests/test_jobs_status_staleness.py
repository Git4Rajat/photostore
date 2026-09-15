"""Regression tests for /api/jobs/status's orphaned-job staleness cutoff.

The cutoff used to only apply to job_type == 'clustering' (mirroring
_has_active_clustering_job's own de-dupe guard). Any other job type left
stuck at status='running' by a hard crash (OOM SIGKILL mid-job, no chance to
write a terminal status) stayed reported as perpetually "in flight" forever,
with no self-healing -- found live on photostore-test as an 'ipwork' row
stuck 'running' since 2026-08-12, keeping the server-processing activity icon
on with nothing left to actually process. The fix removed the
job_type == 'clustering' restriction so the same cutoff now applies to every
job type.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

import app


class _FakeJobsTable:
    """Just enough of azure.data.tables to run jobs_status()'s exact query."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def query_entities(self, filter_str):
        # jobs_status() now fetches the whole 'jobs' partition once (via
        # _jobs_partition_scan_cache) and filters by userId in Python --
        # see that function's comment for why the old server-side "and
        # userId eq X" filter still cost a full partition scan anyway.
        m = re.match(r"PartitionKey eq '([^']*)'$", filter_str)
        assert m, f'unexpected filter: {filter_str}'
        pk = m.group(1)
        return [dict(row) for (p, _), row in self.rows.items() if p == pk]


@pytest.fixture
def jobs_table(monkeypatch):
    table = _FakeJobsTable()
    monkeypatch.setattr(app, 'metadata_table_client', table)
    monkeypatch.setattr(app, '_require_user_id', lambda *a, **k: ('owner', None))
    # Fresh cache per test -- _jobs_partition_scan_cache is a module-level
    # singleton keyed by a constant, so without this, results from one test
    # could leak into the next within the TTL window.
    monkeypatch.setattr(app, '_jobs_partition_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    return table


def _seed_job(table, job_id, job_type, status, *, age_minutes=0, user_id='owner'):
    updated_at = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
    table.upsert_entity({
        'PartitionKey': 'jobs',
        'RowKey': app._job_row_key(job_id),
        'jobId': job_id,
        'userId': user_id,
        'jobType': job_type,
        'status': status,
        'updatedAt': updated_at,
    })


def _poll():
    with app.app.test_request_context('/api/jobs/status'):
        response = app.jobs_status()
    return response.get_json()['jobs']


@pytest.mark.parametrize('job_type', ['ipwork', 'library_clean', 'clustering', 'some_future_type'])
def test_stale_running_job_of_any_type_is_flushed_and_hidden_on_this_poll(jobs_table, job_type):
    job_id = f'{job_type}:owner:stale-1'
    _seed_job(jobs_table, job_id, job_type, 'running', age_minutes=app.CLUSTERING_ACTIVE_JOB_STALE_MINUTES + 5)

    jobs = _poll()

    assert jobs == []
    stored = jobs_table.rows[('jobs', app._job_row_key(job_id))]
    assert stored['status'] == 'failed'
    assert 'did not finish' in stored['error']


def test_flushed_stale_job_then_shows_as_failed_on_the_next_poll(jobs_table):
    job_id = 'ipwork:owner:stale-2'
    _seed_job(jobs_table, job_id, 'ipwork', 'running', age_minutes=app.CLUSTERING_ACTIVE_JOB_STALE_MINUTES + 5)

    first = _poll()
    second = _poll()

    assert first == []
    assert len(second) == 1
    assert second[0]['jobId'] == job_id
    assert second[0]['status'] == 'failed'


def test_recent_running_job_of_any_type_is_left_alone(jobs_table):
    job_id = 'ipwork:owner:fresh-1'
    _seed_job(jobs_table, job_id, 'ipwork', 'running', age_minutes=1)

    jobs = _poll()

    assert len(jobs) == 1
    assert jobs[0]['jobId'] == job_id
    assert jobs[0]['status'] == 'running'
    assert jobs_table.rows[('jobs', app._job_row_key(job_id))]['status'] == 'running'


def test_another_users_stale_job_is_not_touched(jobs_table):
    _seed_job(jobs_table, 'ipwork:other:stale-3', 'ipwork', 'running',
              age_minutes=app.CLUSTERING_ACTIVE_JOB_STALE_MINUTES + 5, user_id='other')

    jobs = _poll()

    assert jobs == []
    assert jobs_table.rows[('jobs', app._job_row_key('ipwork:other:stale-3'))]['status'] == 'running'
