"""Unit tests for the ipwork stale-processing sweep.

A photo can miss the one-time upload-time race between the browser tab and
ipworker (e.g. uploaded while ipworker was admin-stopped, or briefly while
PROCESSING_MODE was 'browser') and end up stuck pending forever: ipworker
only ever sees what's explicitly queued to it, and the browser's own
/upload/processing/pending poll only covers whichever one library a
currently-open tab has active. _sweep_stale_processing_into_ipwork runs on
a background thread inside every ipworker replica and re-offers exactly
that class of orphaned photo -- across every library, not just one -- back
to ipworker, without re-touching photos that are already done or already
have a message legitimately in flight.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import app


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _entity(**overrides):
    base = {
        'RowKey': 'photo.jpg',
        'processing_state': 'active',
        'processing_lease_expires_at': '',
        'last_processing_update': '',
    }
    base.update(overrides)
    return base


class TestEligibleSteps:
    def test_all_done_yields_nothing(self):
        entity = _entity(**{f'{step}_status': 'done' for step in app.IPWORK_STEPS})
        assert app._ipwork_sweep_eligible_steps(entity) == []

    def test_deleted_photo_yields_nothing_even_if_pending(self):
        entity = _entity(processing_state='deleted', thumbnail_status='pending')
        assert app._ipwork_sweep_eligible_steps(entity) == []

    def test_never_touched_step_is_eligible(self):
        entity = _entity(thumbnail_status='', exif_status='done')
        assert 'thumbnail' in app._ipwork_sweep_eligible_steps(entity)
        assert 'exif' not in app._ipwork_sweep_eligible_steps(entity)

    def test_running_with_live_lease_is_left_alone(self):
        entity = _entity(
            face_status='running',
            processing_lease_expires_at=_iso(-60),  # expires 60s in the future
        )
        assert 'face' not in app._ipwork_sweep_eligible_steps(entity)

    def test_running_with_expired_lease_is_eligible(self):
        entity = _entity(
            face_status='running',
            processing_lease_expires_at=_iso(60),  # expired 60s ago
        )
        assert 'face' in app._ipwork_sweep_eligible_steps(entity)

    def test_freshly_queued_step_is_not_resent(self):
        entity = _entity(ocr_status='queued', last_processing_update=_iso(5))
        assert 'ocr' not in app._ipwork_sweep_eligible_steps(entity)

    def test_stale_queued_step_is_eligible(self, monkeypatch):
        monkeypatch.setattr(app, 'IPWORK_SWEEP_STALE_QUEUED_SECONDS', 100)
        entity = _entity(ocr_status='queued', last_processing_update=_iso(200))
        assert 'ocr' in app._ipwork_sweep_eligible_steps(entity)

    def test_ai_vision_raw_no_data_retry_case(self, monkeypatch):
        monkeypatch.setattr(app, '_raw_ai_vision_no_data_should_retry', lambda entity: True)
        entity = _entity(RowKey='photo.cr3', ai_vision_status='no_data')
        assert 'ai_vision' in app._ipwork_sweep_eligible_steps(entity)

    def test_ai_vision_non_retryable_no_data_is_left_done(self, monkeypatch):
        monkeypatch.setattr(app, '_raw_ai_vision_no_data_should_retry', lambda entity: False)
        entity = _entity(ai_vision_status='no_data')
        assert 'ai_vision' not in app._ipwork_sweep_eligible_steps(entity)

    def test_stale_face_embedding_version_is_eligible_despite_done_status(self, monkeypatch):
        """Regression test: a 'done' face_status doesn't mean this photo is
        actually finished if the stored embedding predates the current
        FACE_CLUSTER_EMBEDDING_VERSION -- _browser_processing_pending_item
        already re-queues these for the browser (see
        _browser_processing_face_version_stale). Before this fix, the sweep
        treated 'done' as terminal unconditionally and silently skipped this
        entire class of pending work forever, since it never checked the
        embedding version at all."""
        monkeypatch.setattr(app, 'FACE_CLUSTER_EMBEDDING_VERSION', 'current-version')
        entity = _entity(
            face_status='done',
            processing_metadata=json.dumps({
                'client_face': {'hasData': True, 'modelTaxonomyVersion': 'old-version'},
            }),
        )
        assert 'face' in app._ipwork_sweep_eligible_steps(entity)

    def test_current_face_embedding_version_is_left_done(self, monkeypatch):
        monkeypatch.setattr(app, 'FACE_CLUSTER_EMBEDDING_VERSION', 'current-version')
        entity = _entity(
            face_status='done',
            processing_metadata=json.dumps({
                'client_face': {'hasData': True, 'modelTaxonomyVersion': 'current-version'},
            }),
        )
        assert 'face' not in app._ipwork_sweep_eligible_steps(entity)

    def test_stale_version_with_no_face_data_is_left_done(self, monkeypatch):
        """A 'no_data' result (no faces detected) has nothing to re-embed,
        so a version mismatch shouldn't force it back into the queue."""
        monkeypatch.setattr(app, 'FACE_CLUSTER_EMBEDDING_VERSION', 'current-version')
        entity = _entity(
            face_status='no_data',
            processing_metadata=json.dumps({
                'client_face': {'hasData': False, 'modelTaxonomyVersion': 'old-version'},
            }),
        )
        assert 'face' not in app._ipwork_sweep_eligible_steps(entity)


class TestSweepLoop:
    def test_sweeps_across_every_library_and_skips_done_photos(self, monkeypatch):
        done_except_thumbnail = {f'{s}_status': 'done' for s in app.IPWORK_STEPS if s != 'thumbnail'}
        rows_by_library = {
            'lib-a': [
                _entity(RowKey='stuck.jpg', thumbnail_status='pending', **done_except_thumbnail),
                _entity(RowKey='finished.jpg', **{f'{s}_status': 'done' for s in app.IPWORK_STEPS}),
            ],
            'lib-b': [
                _entity(RowKey='video.mov', thumbnail_status='pending', **done_except_thumbnail),
            ],
        }

        class _FakeLibraryStore:
            def list_all_library_ids(self):
                return list(rows_by_library.keys())

        queued_calls = []

        monkeypatch.setattr(app, 'library_store', _FakeLibraryStore())
        monkeypatch.setattr(app, 'metadata_table_client', object())
        monkeypatch.setattr(
            app, '_query_metadata_rows_for_user',
            lambda library_id, select=None, purpose='metadata': rows_by_library[library_id],
        )
        monkeypatch.setattr(app, 'is_video_file', lambda filename: filename.endswith('.mov'))
        monkeypatch.setattr(
            app, '_queue_ipwork_processing',
            lambda user_id, filename, steps=None: queued_calls.append((user_id, filename, tuple(steps or ()))),
        )

        stats = app._sweep_stale_processing_into_ipwork()

        assert queued_calls == [('lib-a', 'stuck.jpg', ('thumbnail',))]
        assert stats == {'libraries': 2, 'photosQueued': 1, 'stepsQueued': 1}

    def test_no_library_store_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(app, 'library_store', None)
        stats = app._sweep_stale_processing_into_ipwork()
        assert stats == {'libraries': 0, 'photosQueued': 0, 'stepsQueued': 0}

    def test_one_librarys_scan_failure_does_not_stop_the_others(self, monkeypatch):
        class _FakeLibraryStore:
            def list_all_library_ids(self):
                return ['broken-lib', 'lib-b']

        def _scan(library_id, select=None, purpose='metadata'):
            if library_id == 'broken-lib':
                raise RuntimeError('table scan exploded')
            return [_entity(RowKey='stuck.jpg', thumbnail_status='pending')]

        monkeypatch.setattr(app, 'library_store', _FakeLibraryStore())
        monkeypatch.setattr(app, 'metadata_table_client', object())
        monkeypatch.setattr(app, '_query_metadata_rows_for_user', _scan)
        monkeypatch.setattr(app, 'is_video_file', lambda filename: False)
        queued_calls = []
        monkeypatch.setattr(
            app, '_queue_ipwork_processing',
            lambda user_id, filename, steps=None: queued_calls.append((user_id, filename)),
        )

        stats = app._sweep_stale_processing_into_ipwork()

        assert queued_calls == [('lib-b', 'stuck.jpg')]
        assert stats['libraries'] == 2
        assert stats['photosQueued'] == 1
