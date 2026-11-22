"""Per-photo reads of the photo row on the Vision write-back path.

Runs claim -> commit -> release for one wave-owned photo through the real source
write tracking and counts get_entity calls on the metadata table.
"""
import copy
from collections import Counter

import pytest
from azure.core.exceptions import ResourceModifiedError

import storage_utils as su
from tests.test_automatic_index_updates import runtime  # noqa: F401  (fixture)

LIB, NAME = 'A', 'photo.jpg'


class CountingTable:
    def __init__(self, table):
        self.table, self.calls = table, Counter()

    def __getattr__(self, name):
        method = getattr(self.table, name)

        def call(*args, **kwargs):
            self.calls[name] += 1
            return method(*args, **kwargs)
        return call


@pytest.fixture
def photo(runtime, monkeypatch):  # noqa: F811
    from wave_policy import begin_source_change, end_source_change
    counted = CountingTable(runtime.source)
    tracked = su.track_index_source_table(counted, 'metadata')
    monkeypatch.setitem(su._CTX, 'metadata_table_client', tracked)
    monkeypatch.setitem(su._CTX, 'wave_owned', lambda user: True)
    monkeypatch.setattr(su, '_require_context', lambda: None)
    monkeypatch.setattr(su, 'touch_user_search_indexes_state', lambda *a, **k: None)
    monkeypatch.setattr(su, 'publish_source_completion_intent', lambda *a, **k: False)
    runtime.source.upsert_entity(dict(PartitionKey=LIB, RowKey=NAME, anonymousImageId='photo', fileHash='h',
        processingRevision='rev', processingFence='fence', ocr_status='pending'))
    counted.calls.clear()
    token = begin_source_change()
    yield runtime, counted
    end_source_change(token)


def run_wave_source(monkeypatch, during_steps=lambda: None, steps=('ocr',)):
    """The production per-photo path: claim, run the steps, apply, release."""
    import sys
    from types import SimpleNamespace
    import processing_completion
    import processing_generation
    import wave_runtime as runtime
    monkeypatch.setattr(processing_completion, 'completion_capture_enabled', lambda: True)
    monkeypatch.setattr(processing_generation, 'enforcement_enabled', lambda: True)
    monkeypatch.setattr(processing_generation, 'required_steps_for_media', lambda _: list(steps))
    monkeypatch.setitem(sys.modules, 'app', SimpleNamespace(
        _register_ipwork_processors=lambda: None,
        _run_ipwork_steps=lambda *a, **k: during_steps() or {}, jobs_table_client=None))
    runtime.process_source(LIB, NAME, 'rev', lambda: None)


def edited(row, **fields):
    """The row read, edited, still carrying the ETag it was read with."""
    row = copy.copy(row)
    row.update(fields)
    return row


def change_row(runtime, **fields):
    """Another writer changes the stored row while the steps run (a new ETag)."""
    return lambda: runtime.source.update_entity(dict(PartitionKey=LIB, RowKey=NAME, **fields), mode='merge')


def test_one_photo_row_read_per_photo(photo, monkeypatch):
    """Claim reads the row once; apply, the commit's change tracking and release read nothing more (was 5)."""
    runtime, counted = photo
    run_wave_source(monkeypatch)
    assert counted.calls['get_entity'] == 1
    assert runtime.source.rows[LIB, NAME]['processing_lease_owner'] == ''


@pytest.mark.parametrize('change', [
    {'processingRevision': 'successor'},
    {'processing_lease_owner': 'someone-else'},
    {'processingFence': 'new-attempt'},
    {'processing_state': 'deleted'},
])
def test_row_changed_after_claim_still_blocks_the_result(photo, monkeypatch, change):
    from processing_completion import StaleProcessingResult
    runtime, counted = photo
    with pytest.raises((StaleProcessingResult, RuntimeError)):
        run_wave_source(monkeypatch, change_row(runtime, **change))
    row = runtime.source.rows[LIB, NAME]
    assert row['ocr_status'] == 'pending'  # nothing of this attempt was written
    for key, value in change.items():
        assert row[key] == value


def test_failed_apply_still_releases_the_lease(photo, monkeypatch):
    runtime, counted = photo

    def fail():
        raise RuntimeError('model failed')
    with pytest.raises(RuntimeError, match='model failed'):
        run_wave_source(monkeypatch, fail)
    assert runtime.source.rows[LIB, NAME]['processing_lease_owner'] == ''


def test_claim_older_than_the_heartbeat_is_not_reused(photo, monkeypatch):
    import wave_runtime
    runtime, counted = photo
    monkeypatch.setattr(wave_runtime, 'SOURCE_ROW_REUSE_SECONDS', 0)
    run_wave_source(monkeypatch)
    assert counted.calls['get_entity'] == 2  # claim + apply's own read
    assert runtime.source.rows[LIB, NAME]['processing_lease_owner'] == ''


def test_cached_row_never_hides_a_change_from_a_conditional_write(photo):
    from automatic_index_updates import coalesced_source_reservations
    from azure.core import MatchConditions
    from azure.core.exceptions import ResourceModifiedError
    runtime, counted = photo
    table = su._CTX['metadata_table_client']
    with coalesced_source_reservations():
        row = table.get_entity(LIB, NAME)
        runtime.source.update_entity(dict(PartitionKey=LIB, RowKey=NAME, caption='edited elsewhere'), mode='merge')
        with pytest.raises(ResourceModifiedError):
            table.update_entity(edited(row, ocr_status='done'), mode='merge', match_condition=MatchConditions.IfNotModified)
    assert runtime.source.rows[LIB, NAME]['ocr_status'] == 'pending'


def test_row_cache_is_off_outside_a_wave_scope(photo):
    from azure.core import MatchConditions
    runtime, counted = photo
    table = su._CTX['metadata_table_client']
    row = table.get_entity(LIB, NAME)
    table.update_entity(edited(row, ocr_status='done'), mode='merge', match_condition=MatchConditions.IfNotModified)
    assert counted.calls['get_entity'] == 2  # the read, and the tracking's own read before the write


def test_cached_and_fresh_reads_reserve_the_same_kinds(photo, monkeypatch):
    from automatic_index_updates import coalesced_source_reservations
    from azure.core import MatchConditions
    runtime, counted = photo
    table = su._CTX['metadata_table_client']
    kinds = {}
    for label, scoped in (('fresh', False), ('cached', True)):
        seen = []
        monkeypatch.setattr('automatic_index_updates.mutation_targets',
                            lambda *a, _orig=__import__('automatic_index_updates').mutation_targets, _seen=seen:
                            (_seen.append(_orig(*a)[1]) or _orig(*a)))
        runtime.source.upsert_entity(dict(PartitionKey=LIB, RowKey=NAME, anonymousImageId='photo', caption='cat'))
        row = table.get_entity(LIB, NAME)
        update = edited(row, ocr_status='done', photoEmbedding='[1]')
        if scoped:
            with coalesced_source_reservations():
                row = table.get_entity(LIB, NAME)
                table.update_entity(edited(row, ocr_status='done', photoEmbedding='[1]'), mode='merge',
                                    match_condition=MatchConditions.IfNotModified)
        else:
            table.update_entity(update, mode='merge', match_condition=MatchConditions.IfNotModified)
        kinds[label] = seen
    assert kinds['cached'][-1] == kinds['fresh'][-1]
