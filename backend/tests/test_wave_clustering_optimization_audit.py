"""Fault boundaries for wave-scoped assignment; included in image wave gates."""
import pytest

pytest.importorskip('faiss')

from azure.core.exceptions import ResourceModifiedError
from test_wave_assignment_journal import setup, comparable
from test_wave_assignment_files import run
from clustering_reference_oracle import assignment_snapshot


@pytest.mark.parametrize('failure', ['admission', 'coverage_finish'])
def test_scoped_assignment_preserves_admission_and_failed_coverage(tmp_path, monkeypatch, failure):
    import app
    import automatic_index_updates as automatic
    import storage_utils as su
    from tests.test_automatic_index_updates import Table
    from wave_policy import source_change_active

    h, snapshots, store, frozen, leases = setup()
    initial = comparable(assignment_snapshot(h))
    dirty = Table()
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(su, '_CTX', {'search_index_dirty_table_client': dirty,
                                   'wave_owned': lambda _: True})
    monkeypatch.setattr(app, 'jobs_table_client', Table())
    monkeypatch.setattr(automatic, 'schedule', lambda *args: pytest.fail('Independent wave maintenance'))
    h.assigner.face_table = automatic.SourceTable(h.faces, 'face')
    h.assigner.person_table = automatic.SourceTable(h.people, 'person')

    if failure == 'admission':
        def blocked(*args):
            raise automatic.SourceBusy('operator admission fence')
        monkeypatch.setattr(automatic, 'check_source_admission', blocked)
        with pytest.raises(automatic.SourceBusy, match='operator admission fence'):
            run(h, snapshots, store, frozen, leases, tmp_path)
        assert comparable(assignment_snapshot(h)) == initial
        assert not dirty.rows
    else:
        def unavailable(*args, **kwargs):
            raise OSError('coverage finish unavailable')
        monkeypatch.setattr(automatic.CoverageStore, 'finish', unavailable)
        with pytest.raises(ExceptionGroup, match='source finalization failed'):
            run(h, snapshots, store, frozen, leases, tmp_path)
        coverage = automatic.CoverageStore(dirty)
        assert coverage.control('u', 'people_embedding')['active'] == 1
        assert coverage.reservations('u', 'people_embedding')
        with pytest.raises(automatic.SourceBusy):
            coverage.capture('u', 'people_embedding')
    assert not source_change_active()
    assert store._get(f"assignment!{frozen['waveId']}!complete") is None


def test_scoped_assignment_does_not_upgrade_etag_after_a_foreground_race(tmp_path, monkeypatch):
    import app
    import automatic_index_updates as automatic
    import storage_utils as su
    from tests.test_automatic_index_updates import Table
    from wave_policy import source_change_active
    from azure.core import MatchConditions
    from azure.data.tables import UpdateMode

    h, snapshots, store, frozen, leases = setup()
    dirty = Table()
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', True)
    monkeypatch.setattr(su, '_CTX', {'search_index_dirty_table_client': dirty,
                                   'wave_owned': lambda _: True})
    monkeypatch.setattr(app, 'jobs_table_client', Table())
    monkeypatch.setattr(automatic, 'schedule', lambda *args: pytest.fail('Independent wave maintenance'))
    h.assigner.face_table = automatic.SourceTable(h.faces, 'face')
    h.assigner.person_table = automatic.SourceTable(h.people, 'person')
    update = h.faces.update_entity
    edited = []
    def race(row, **options):
        if not edited and row.get('assignmentDecisionId'):
            edited.append(row['RowKey'])
            live = h.faces.get_entity('u', row['RowKey'])
            update(dict(live, personId='bob', reviewStatus='confirmed'),
                   mode=UpdateMode.MERGE, etag=live.metadata['etag'],
                   match_condition=MatchConditions.IfNotModified)
        return update(row, **options)
    monkeypatch.setattr(h.faces, 'update_entity', race)
    with pytest.raises(ResourceModifiedError):
        run(h, snapshots, store, frozen, leases, tmp_path)
    assert len(edited) == 1
    live = h.faces.get_entity('u', edited[0])
    assert live['personId'] == 'bob' and live['reviewStatus'] == 'confirmed'
    assert not live.get('assignmentDecisionId')
    assert not source_change_active()
    assert automatic.CoverageStore(dirty).control('u', 'people_embedding')['active'] == 0
    assert store._get(f"assignment!{frozen['waveId']}!complete") is None
