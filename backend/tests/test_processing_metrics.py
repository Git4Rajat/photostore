"""Exact attribution under overlap, bounded labels, timers and no context leaks."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import threading
from types import SimpleNamespace

import pytest
import perf_instrumentation as perf
import processing_diagnostics as diagnostics
import processing_metrics as metrics
from test_processing_diagnostics import records


def metadata_reads(count):
    for _ in range(count):
        perf.record_io('table', 'GET', "/photometadata(PartitionKey='secret',RowKey='private.jpg')", 200, 2, 12)


def test_parallel_images_have_exclusive_counts_and_wave_includes_coordinator(caplog):
    store = SimpleNamespace(library='private-library')
    barrier = threading.Barrier(2)
    members = [dict(RowKey='member-a', filename='private-a.jpg'), dict(RowKey='member-b', filename='private-a.jpg')]
    @diagnostics.vision_group_summary
    def group(store, wave_id, members):
        for member in members:
            with diagnostics.image_scope(member['RowKey'], 'inspection'):
                metadata_reads(1)
        def process(member, count):
            with diagnostics.image_scope(member['RowKey'], 'processing'), metrics.purpose('claim'):
                barrier.wait(timeout=5)
                metadata_reads(count)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(copy_context().run, process, member, count)
                     for member, count in zip(members, (3, 7))]
            for task in tasks: task.result()
        for member in members:
            with diagnostics.image_scope(member['RowKey'], 'qualification'):
                metadata_reads(1)
        return [(member, ('intent', 'proof'), False) for member in members]
    with metrics.accounting() as wave:
        metadata_reads(2)  # Deliberately shared coordinator work.
        group(store, 'wave', members)
    rows = [row for row in records(caplog) if row['event'] == 'vision_image_summary']
    assert sorted(row['metadata_point_reads'] for row in rows) == [5, 9]
    assert len({row['member_id'] for row in rows}) == 2  # Same filename, distinct source members.
    assert sorted(row['metadata_claim_reads'] for row in rows) == [3, 7]
    assert all(row['metadata_inspection_reads'] == row['metadata_qualification_reads'] == 1 for row in rows)
    assert wave.snapshot()['metadata_point_reads'] == 16
    assert wave.snapshot()['source_attempts'] == 2
    assert all(row['source_attempts'] == row['image_passes'] == 1 for row in rows)
    assert 'private' not in json.dumps(rows)


def test_raw_heartbeat_threads_inherit_only_the_accounting_context():
    with metrics.accounting() as wave, metrics.accounting() as image:
        thread = threading.Thread(target=metrics.inherit(lambda: metadata_reads(2)))
        thread.start(); thread.join(timeout=5)
    assert image.snapshot()['metadata_heartbeat_reads'] == 2
    assert wave.snapshot()['metadata_point_reads'] == 2
    metadata_reads(9)
    assert image.snapshot()['metadata_point_reads'] == 2


def test_error_resets_purpose_and_counter_and_keeps_bounded_labels():
    with metrics.accounting() as counter:
        with pytest.raises(ValueError), metrics.purpose('claim'):
            metadata_reads(1)
            raise ValueError('failure')
        with metrics.purpose('private-user-generated-label'):
            metadata_reads(1)
        metrics.add(private_user_generated_field=100)
    values = counter.snapshot()
    assert values['metadata_claim_reads'] == values['metadata_other_reads'] == 1
    assert all('private' not in key for key in values)
    metadata_reads(3)
    assert counter.snapshot() == values


def test_timer_accounts_failure_and_distinguishes_flush_from_fsync():
    with metrics.accounting() as counter:
        with pytest.raises(OSError), metrics.timer('journal_flush'):
            with metrics.timer('journal_fsync'):
                pass
            raise OSError('commit failed')
    values = counter.snapshot()
    assert values['journal_flushes'] == values['journal_fsyncs'] == 1
    assert values['journal_flush_errors'] == 1 and values['journal_fsync_errors'] == 0
    assert values['journal_flush_ms'] >= values['journal_fsync_ms'] >= 0


def test_batch_counters_measure_queries_rows_and_absence_without_extra_reads():
    from table_reads import read_entities
    from test_table_reads import QueryRows
    table = QueryRows()
    keys = [str(i) for i in range(20)]
    for key in keys[:-1]: table.create_entity(dict(PartitionKey='lib', RowKey=key))
    with metrics.accounting() as counter:
        rows = read_entities(table, 'lib', keys)
    values = counter.snapshot()
    assert values['batch_requests'] == 1 and values['batch_queries'] == len(table.queries) == 2
    assert values['batch_keys'] == 20 and values['batch_rows'] == 19 and values['batch_missing'] == 1
    assert table.reads == 0 and rows['19'] is None


def test_vision_attempt_summary_is_emitted_on_failure(caplog):
    store = SimpleNamespace(library='lib')
    @diagnostics.vision_wave_summary
    def attempt(leases, work, store, library, wave_id):
        metadata_reads(2)
        raise ValueError('conflict')
    with pytest.raises(ValueError): attempt(None, None, store, 'lib', 'wave')
    row, = [row for row in records(caplog) if row['event'] == 'vision_wave_attempt_summary']
    assert row['outcome'] == 'error' and not row['vision_complete']
    assert row['metadata_point_reads'] == 2 and row['measurement_scope'] == 'attempt'


def test_sdk_parenthesized_query_endpoint_is_not_a_point_read():
    with metrics.accounting() as counter:
        metrics.record_io('table', 'GET', '/photometadata()?$filter=private', 200, 2, 10)
        metadata_reads(1)
    assert counter.snapshot()['metadata_query_requests'] == 1
    assert counter.snapshot()['metadata_point_reads'] == 1


def test_unvisited_deadline_tail_does_not_emit_image_summary(caplog):
    store = SimpleNamespace(library='private-library')
    members = [dict(RowKey=k, filename=k + '.jpg') for k in ('visited', 'tail')]
    @diagnostics.vision_group_summary
    def group(store, wave_id, members):
        with diagnostics.image_scope('visited', 'inspection'):
            pass
        return []
    group(store, 'wave', members)
    rows = [r for r in records(caplog) if r['event'] == 'vision_image_summary']
    assert len(rows) == 1
    assert rows[0]['image_inspections'] == rows[0]['image_passes'] == 1


def test_source_boundary_timer_uses_explicit_call_count_on_failure():
    with metrics.accounting() as counter:
        with pytest.raises(RuntimeError):
            with metrics.timer('vision_source_steps', count_name='vision_source_steps_calls'):
                raise RuntimeError('model failed')
    values = counter.snapshot()
    assert values['vision_source_steps_calls'] == values['vision_source_steps_errors'] == 1
    assert 'vision_source_steps_ms' in counter.values
