"""Request/job scopes, storage round-trip attribution, duplicate detection,
correlation headers and the client-report endpoint."""
import logging

import pytest

import app as app_module
import perf_instrumentation as perf


def test_scope_counts_io_and_flags_duplicate_calls(caplog):
    caplog.set_level(logging.INFO, logger='perf')
    with perf.scope('unit.job', user='u1') as sc:
        for _ in range(4):
            perf.record_io('table', 'GET', "/photos(PartitionKey='u1',RowKey='a.jpg')", 200, 12.0, 100)
        perf.record_io('blob', 'GET', '/lexical-index/u1-sort.json.gz', 200, 300.0, 5_000_000)
    assert sc.io_count == 5
    assert sc.io_bytes == 5_000_400
    text = caplog.text
    assert 'event=scope_summary name=unit.job' in text
    assert 'io_calls=5' in text
    # four identical point reads in one job = duplicate work
    assert 'event=dup_io' in text and 'times=4' in text
    # the slowest op is listed first and ids are collapsed into a stable label
    assert 'blob:GET:lexical-index' in text


def test_op_labels_are_low_cardinality():
    a = perf._op_label('table', 'GET', "/photos(PartitionKey='u1',RowKey='a.jpg')")
    b = perf._op_label('table', 'GET', "/photos(PartitionKey='u2',RowKey='zzz')")
    assert a == b == 'table:GET:photos(..)'
    assert perf._op_label('blob', 'PUT', '/c/b') == 'blob:PUT:c'


def test_step_reports_io_made_inside_it(caplog):
    caplog.set_level(logging.INFO, logger='perf')
    with perf.scope('unit.steps'):
        with perf.step('phase.one'):
            perf.record_io('table', 'POST', '/t', 200, 5.0, 10)
            perf.record_io('table', 'POST', '/t', 200, 5.0, 10)
    assert 'event=step name=phase.one io_calls=2' in caplog.text


def test_io_in_thread_pool_workers_is_attributed_to_the_scope():
    from concurrent.futures import ThreadPoolExecutor
    perf._propagate_context_into_pools()
    with perf.scope('unit.pool') as sc:
        with ThreadPoolExecutor(max_workers=3) as ex:
            list(ex.map(lambda i: perf.record_io('blob', 'GET', f'/c/{i}', 200, 1.0, 1), range(6)))
    assert sc.io_count == 6


def test_requests_get_correlation_and_server_timing_headers():
    client = app_module.app.test_client()
    res = client.get('/health', headers={'X-Request-ID': 'abc123', 'X-Client-View': 'albums'})
    assert res.headers['X-Request-ID'] == 'abc123'
    assert 'app;dur=' in res.headers['Server-Timing']
    assert 'X-Request-ID' in res.headers['Access-Control-Expose-Headers']
    assert 'Server-Timing' in res.headers['Access-Control-Expose-Headers']


def test_client_report_is_logged_without_query_strings(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='perf')
    monkeypatch.setattr(app_module, '_require_user_id', lambda *a, **k: ('u1', None))
    res = app_module.app.test_client().post('/api/perf/client', json={
        'session': 's-1',
        'events': [
            {'t': 'req', 'method': 'GET', 'path': '/api/photos?token=SECRET', 'status': 200, 'ms': 123, 'rid': 'r1', 'evil': 'x'},
            {'t': 'dup', 'kind': 'blob', 'key': 'thumbs/a.jpg?sig=SECRET', 'n': 2},
            {'t': 'nope', 'x': 1},
            'garbage',
        ],
    })
    assert res.get_json() == {'ok': True, 'accepted': 2}
    assert 'event=client_req user=u1 sess=s-1 method=GET path=/api/photos status=200 ms=123 rid=r1' in caplog.text
    assert 'SECRET' not in caplog.text and 'evil' not in caplog.text
