"""Real FAISS oracle parity for the write-ahead wave path, including lost replies."""
from copy import deepcopy
import json

import pytest
pytest.importorskip('faiss')

import faiss_assignment as assignment
import wave_assignment as wave
from clustering_reference_oracle import assignment_snapshot, run_single_pass_reference
from test_clustering_reference_oracle import seeded, assert_fixture_semantics
from test_index_work import EtagTable
from test_upload_waves import AtomicTable, close, finish_scan
from execution_lease import ExecutionLeases, execution_scope, OwnershipLost
from upload_waves import ShadowWaves


class Rows(EtagTable):
    def __init__(self, original):
        super().__init__()
        self.fail = None
        self.after_write = None
        for row in original.rows.values():
            self.create_entity(deepcopy(row))

    def query_entities(self, query):
        library = query.split("'")[1].replace("''", "'")
        for key in sorted(self.rows):
            if key[0] == library:
                yield dict(self.rows[key])

    def update_entity(self, row, **options):
        super().update_entity(row, **options)
        self._after(row)

    def create_entity(self, row):
        super().create_entity(row)
        self._after(row)

    def _after(self, row):
        if self.after_write:
            self.after_write(row)
        if self.fail:
            error, self.fail = self.fail, None
            raise error


@pytest.fixture(autouse=True)
def runtime():
    assignment.invalidate()
    yield
    assignment.invalidate()


def setup():
    h, ids = seeded()
    for attr, field in [('faces','face_table'), ('people','person_table'),
                        ('embeddings','embedding_table'), ('members','member_table')]:
        table = Rows(getattr(h, attr))
        setattr(h, attr, table)
        setattr(h.assigner, field, table)
    snapshots = [deepcopy(h.faces.rows['u', face_id]) for face_id in ids]
    store = ShadowWaves(AtomicTable(), 'u')
    opened = store.open_session('s', 0); close(store, 's', opened)
    frozen = finish_scan(store, opened['waveId'])
    leases = ExecutionLeases(store.table, partition=store.partition)
    return h, snapshots, store, frozen, leases


def execute(h, snapshots, store, frozen, leases, *, page_size=3, after_capture=None):
    ownership = leases.claim('u', frozen['waveId'], 'cluster', frozen['frozenRevision'])
    def check():
        leases.renew(ownership)
    try:
        with execution_scope(leases, ownership, store, check):
            if after_capture:
                after_capture(ownership)
            return wave.run_frozen_assignment(store, frozen, h.assigner,
                lambda: iter(deepcopy(snapshots)), check, page_size=page_size)
    finally:
        try:
            leases.release(ownership)
        except OwnershipLost:
            pass


def comparable(snapshot):
    # Photo metadata projections are deliberately deferred to the index stage.
    return {key: value for key, value in snapshot.items() if key != 'projectedFiles'}


def oracle():
    reference, ids = seeded()
    result = run_single_pass_reference(reference, ids)
    assert_fixture_semantics(result)
    assignment.invalidate()
    return comparable(result)


@pytest.mark.parametrize('page_size', [1,2,3,128])
def test_streamed_journaled_assignments_equal_the_single_pass_oracle(page_size):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    receipt = execute(h, snapshots, store, frozen, leases, page_size=page_size)
    assert comparable(assignment_snapshot(h)) == expected
    assert not h.metadata  # Clustering performs no metadata/index projection.
    journal = wave.WaveAssignmentJournal(store, frozen['waveId'], lambda: None)
    assert len(receipt) == 64
    assert journal.get('complete')['faceCount'] == len(snapshots)
    assert all(journal.decision(i) for i in range(len(snapshots)))


@pytest.mark.parametrize('failure', ['person','member','face','decision','applied'])
def test_lost_replies_resume_recorded_decisions_without_redeciding(failure, monkeypatch):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    if failure in ('person','member','face'):
        getattr(h, {'person':'people','member':'members','face':'faces'}[failure]).fail = OSError('lost reply')
    else:
        original = wave.WaveAssignmentJournal.put
        fired = [False]
        def put(self, key, body):
            result = original(self, key, body)
            if key.startswith(failure+'!') and not fired[0]:
                fired[0] = True
                raise OSError('lost reply')
            return result
        monkeypatch.setattr(wave.WaveAssignmentJournal, 'put', put)
    with pytest.raises(OSError, match='lost reply'):
        execute(h, snapshots, store, frozen, leases)
    # A durable decision exists before any possibly committed materialization.
    journal = wave.WaveAssignmentJournal(store, frozen['waveId'], lambda: None)
    assert journal.decision(0)
    original_decide = wave._decide
    def no_redecide(assigner, library, face, *args):
        assert face['RowKey'] != journal.decision(0)['faceId']
        return original_decide(assigner, library, face, *args)
    monkeypatch.setattr(wave, '_decide', no_redecide)
    execute(h, snapshots, store, frozen, leases, page_size=1)
    assert comparable(assignment_snapshot(h)) == expected


def test_completed_replay_has_no_materialization_or_native_duplicate_inserts(monkeypatch):
    h, snapshots, store, frozen, leases = setup()
    first = execute(h, snapshots, store, frozen, leases)
    before = comparable(assignment_snapshot(h))
    for table in (h.faces, h.people, h.members):
        table.after_write = lambda row: pytest.fail('reapplied committed materialization')
    real_upsert = wave.LiveFaceIndex.upsert
    inserted = set()
    def upsert(runtime, face_id, *args):
        assert face_id not in inserted
        inserted.add(face_id)
        return real_upsert(runtime, face_id, *args)
    monkeypatch.setattr(wave.LiveFaceIndex, 'upsert', upsert)
    monkeypatch.setattr(wave, '_decide', lambda *a: pytest.fail('recomputed committed decision'))
    assert execute(h, snapshots, store, frozen, leases, page_size=2) == first
    assert comparable(assignment_snapshot(h)) == before


def test_resume_reads_the_durable_manifest_not_the_new_input_iterator():
    h, snapshots, store, frozen, leases = setup()
    execute(h, snapshots, store, frozen, leases)
    # The initial source iterator is deliberately unusable after publication.
    class Poison:
        def __deepcopy__(self, memo):
            pytest.fail('reloaded input instead of the durable frozen manifest')
    execute(h, Poison(), store, frozen, leases)


def test_policy_change_rejects_resume_before_effects():
    from dataclasses import replace
    h, snapshots, store, frozen, leases = setup()
    execute(h, snapshots, store, frozen, leases)
    h.assigner.config = replace(h.assigner.config, threshold=.1)
    with pytest.raises(ValueError, match='policy changed'):
        execute(h, snapshots, store, frozen, leases)


def test_partial_face_write_cannot_overwrite_new_source_on_retry():
    h, snapshots, store, frozen, leases = setup()
    h.people.fail = OSError('stop before face')
    with pytest.raises(OSError):
        execute(h, snapshots, store, frozen, leases)
    h.faces.rows['u','a']['processingRevision'] = 'replacement'
    with pytest.raises(ValueError, match='source or curation changed'):
        execute(h, snapshots, store, frozen, leases)
    assert not h.faces.rows['u','a'].get('personId')


def test_late_page_failure_does_not_publish_manifest_or_assignment(monkeypatch):
    h, snapshots, store, frozen, leases = setup()
    class Broken:
        def __deepcopy__(self, memo):
            def rows():
                yield snapshots[0]
                raise OSError('late input page')
            return rows()
    with pytest.raises(OSError, match='late input page'):
        execute(h, Broken(), store, frozen, leases)
    journal = wave.WaveAssignmentJournal(store, frozen['waveId'], lambda: None)
    assert journal.get('manifest') is None and journal.get('complete') is None
    assert journal.decision(0) is None


def test_decision_commit_is_atomically_fenced_when_an_owner_is_replaced():
    h, snapshots, store, frozen, leases = setup()
    now = [1000.0]; leases.clock = lambda: now[0]
    # Replace the owner just before a decision transaction reaches the table.
    original = wave.WaveAssignmentJournal.put
    def steal(ownership):
        def put(self, key, payload):
            if key.startswith('decision!'):
                def replace():
                    now[0] += 1000
                    leases.claim('u', frozen['waveId'], 'cluster', frozen['frozenRevision'])
                store.table.before_commit = replace
            return original(self, key, payload)
        self_patch = pytest.MonkeyPatch()
        self_patch.setattr(wave.WaveAssignmentJournal, 'put', put)
        patches.append(self_patch)
    patches=[]
    try:
        with pytest.raises(OwnershipLost):
            execute(h, snapshots, store, frozen, leases, after_capture=steal)
    finally:
        for patch in patches: patch.undo()
    journal = wave.WaveAssignmentJournal(store, frozen['waveId'], lambda: None)
    assert journal.decision(0) is None
    assert not h.faces.rows['u','a'].get('personId')


@pytest.mark.parametrize('failure_point', ['before_decision','after_effects_before_marker','after_marker_before_runtime'])
def test_crash_boundaries_recover_without_changing_person_creation(failure_point, monkeypatch):
    expected = oracle()
    h, snapshots, store, frozen, leases = setup()
    fired = [False]
    if failure_point == 'before_decision':
        original = wave.WaveAssignmentJournal.put
        def crash(self, key, value):
            if key.startswith('decision!') and not fired[0]:
                fired[0] = True
                raise OSError('crash boundary')
            return original(self,key,value)
        monkeypatch.setattr(wave.WaveAssignmentJournal,'put',crash)
    elif failure_point == 'after_effects_before_marker':
        original = wave.WaveAssignmentJournal.put
        def crash(self,key,value):
            if key.startswith('applied!') and not fired[0]:
                fired[0] = True
                raise OSError('crash boundary')
            return original(self,key,value)
        monkeypatch.setattr(wave.WaveAssignmentJournal,'put',crash)
    else:
        original = wave._replay
        def crash(*args):
            if not fired[0]:
                fired[0] = True
                raise OSError('crash boundary')
            return original(*args)
        monkeypatch.setattr(wave,'_replay',crash)
    with pytest.raises(OSError): execute(h,snapshots,store,frozen,leases)
    execute(h,snapshots,store,frozen,leases,page_size=2)
    assert comparable(assignment_snapshot(h)) == expected


def test_native_delta_compaction_preserves_oracle_decisions():
    from faiss_assignment import AssignmentConfig
    config = AssignmentConfig(delta_limit=1)
    reference, ids = seeded(config)
    expected = comparable(run_single_pass_reference(reference,ids))
    assignment.invalidate()
    h,snapshots,store,frozen,leases=setup()
    h.assigner.config=config
    execute(h,snapshots,store,frozen,leases,page_size=1)
    assert comparable(assignment_snapshot(h)) == expected


def test_manifest_ingestion_is_single_pass_and_disk_ordered(monkeypatch):
    h,snapshots,store,frozen,leases=setup()
    seen=[]
    original=wave.Workspace.input
    def ingest(workspace,row):
        seen.append(row['RowKey'])
        return original(workspace,row)
    monkeypatch.setattr(wave.Workspace,'input',ingest)
    class Once:
        def __deepcopy__(self,memo):
            def rows():
                yield from reversed(snapshots)
            return rows()
    execute(h,Once(),store,frozen,leases,page_size=2)
    assert seen == [r['RowKey'] for r in reversed(snapshots)]
    journal=wave.WaveAssignmentJournal(store,frozen['waveId'],lambda:None)
    actual=[journal.decision(i)['faceId'] for i in range(len(snapshots))]
    from clustering_wave import canonical_order
    assert actual == canonical_order({row['RowKey']:row for row in snapshots})


def test_duplicate_input_refuses_manifest_publication():
    h,snapshots,store,frozen,leases=setup()
    snapshots.append(deepcopy(snapshots[0]))
    with pytest.raises(ValueError,match='Duplicate face'):
        execute(h,snapshots,store,frozen,leases)
    assert not any('!decision!' in key[1] for key in store.table.rows)


def test_materialization_preserves_racing_person_name_curation():
    h,snapshots,store,frozen,leases=setup()
    original=wave._apply
    def rename(assigner,library,face,decision,*args):
        if decision.get('personId') == 'alice':
            h.people.rows['u','alice']['name']='Renamed by user'
            h.people.versions['u','alice']+=1
        return original(assigner,library,face,decision,*args)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(wave,'_apply',rename)
        execute(h,snapshots,store,frozen,leases)
    assert h.people.rows['u','alice']['name']=='Renamed by user'


def test_orphaned_deterministic_person_from_a_legacy_partial_write_is_reused():
    reference, ids = seeded()
    person_id = reference.assigner.person_id('u','b')
    reference.person(person_id, (), rep=(0,0,1), name='Prior partial person')
    expected = comparable(run_single_pass_reference(reference,ids))
    assignment.invalidate()
    h,snapshots,store,frozen,leases=setup()
    h.people.create_entity(dict(PartitionKey='u',RowKey=person_id,name='Prior partial person',
        faceIds='[]',repEmbedding='[0,0,1]'))
    execute(h,snapshots,store,frozen,leases)
    assert comparable(assignment_snapshot(h)) == expected


def test_input_object_retention_stays_bounded_across_many_manifest_pages():
    h,_,store,frozen,leases=setup()
    tracked={'live':0,'peak':0}
    class Face(dict):
        def __init__(self,*a,**kw):
            super().__init__(*a,**kw)
            tracked['live']+=1
            tracked['peak']=max(tracked['peak'],tracked['live'])
        def __del__(self): tracked['live']-=1
    class Streaming:
        def __deepcopy__(self,memo):
            def rows():
                for i in range(257):
                    yield Face(PartitionKey='u',RowKey=f'face-{i:04d}',filename=f'{i:04d}.jpg',
                        embedding='[1,0]',embeddingVersion='v1',alignmentMethod='5pt',rejected=True)
            return rows()
    execute(h,Streaming(),store,frozen,leases,page_size=7)
    assert tracked['peak'] <= 2
    journal=wave.WaveAssignmentJournal(store,frozen['waveId'],lambda:None)
    assert journal.get('complete')['faceCount']==257
    assert all(size<=20 for query,size in store.table.queries)


def test_journaled_wave_ignores_legacy_runtime_checkpoint_restore(monkeypatch,tmp_path):
    from dataclasses import replace
    h,snapshots,store,frozen,leases=setup()
    h.assigner.config=replace(h.assigner.config,checkpoint_dir=str(tmp_path/'checkpoints'))
    monkeypatch.setattr(wave.LiveFaceIndex,'restore_checkpoint',
        lambda *a,**k:pytest.fail('loaded a legacy mutable checkpoint'))
    execute(h,snapshots,store,frozen,leases)
    execute(h,snapshots,store,frozen,leases,page_size=2)


def test_deleted_matched_person_is_not_recreated_during_recovery(monkeypatch):
    h,snapshots,store,frozen,leases=setup()
    original=wave.WaveAssignmentJournal.put
    fired=[False]
    def crash(self,key,payload):
        result=original(self,key,payload)
        if key.startswith('decision!') and not fired[0]:
            fired[0]=True
            raise OSError('before applying')
        return result
    monkeypatch.setattr(wave.WaveAssignmentJournal,'put',crash)
    with pytest.raises(OSError): execute(h,snapshots,store,frozen,leases)
    del h.people.rows['u','alice']
    with pytest.raises(ValueError,match='refusing resurrection'):
        execute(h,snapshots,store,frozen,leases)
    assert ('u','alice') not in h.people.rows


def test_missing_frozen_manifest_entry_rejects_resume_before_effects(monkeypatch):
    h,snapshots,store,frozen,leases=setup()
    original=wave.WaveAssignmentJournal.put
    def stop(self,key,payload):
        if key.startswith('decision!'):
            raise OSError('before effects')
        return original(self,key,payload)
    with monkeypatch.context() as patch:
        patch.setattr(wave.WaveAssignmentJournal,'put',stop)
        with pytest.raises(OSError): execute(h,snapshots,store,frozen,leases)
    key=next(key for key in store.table.rows if '!input!' in key[1])
    del store.table.rows[key]
    with pytest.raises(ValueError,match='coverage mismatch'):
        execute(h,snapshots,store,frozen,leases)
    assert not h.faces.rows['u','a'].get('personId')


def test_changed_baseline_cannot_rebind_an_interrupted_initial_capture():
    h,snapshots,store,frozen,leases=setup()
    class Broken:
        def __deepcopy__(self,memo):
            def rows():
                yield snapshots[0]
                raise OSError('initial capture')
            return rows()
    with pytest.raises(OSError): execute(h,Broken(),store,frozen,leases)
    h.assignment_revision+=1
    with pytest.raises(ValueError,match='rebound'):
        execute(h,snapshots,store,frozen,leases)
    assert not h.faces.rows['u','a'].get('personId')


def test_progress_reports_only_the_applied_prefix(monkeypatch):
    h,snapshots,store,frozen,leases=setup()
    recorded=[]
    original=wave.run_frozen_assignment
    def runner(*args,**kwargs):
        def progress(count):
            journal=wave.WaveAssignmentJournal(store,frozen['waveId'],lambda:None)
            for position in range(count):
                assert journal.applied(position,wave.digest(journal.decision(position)))
            recorded.append(count)
        kwargs['progress']=progress
        return original(*args,**kwargs)
    monkeypatch.setattr(wave,'run_frozen_assignment',runner)
    execute(h,snapshots,store,frozen,leases,page_size=2)
    assert recorded==[2,4,6,8,9]
