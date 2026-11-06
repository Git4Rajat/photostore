"""Bounded membership encoding with legacy recovery and live-write semantics."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
pytest.importorskip('faiss')

import wave_assignment as wave
import wave_assignment_resume as resume
from execution_lease import execution_scope
from clustering_reference_oracle import assignment_snapshot, run_single_pass_reference
from test_clustering_reference_oracle import seeded
from test_wave_assignment_journal import setup, comparable, oracle


def run(h, snapshots, store, frozen, leases, *, local=False, stop=False, page_size=1):
    owner=leases.claim('u',frozen['waveId'],'cluster',frozen['frozenRevision'])
    try:
        with execution_scope(leases,owner,store,lambda:leases.check(owner)):
            return wave.run_frozen_assignment(store,frozen,h.assigner,
                lambda:iter(deepcopy(snapshots)),lambda:None,page_size=page_size,
                local_journal=local,should_yield=lambda:stop)
    finally:
        leases.release(owner)


@pytest.mark.parametrize('local',[False,True])
def test_large_person_snapshot_and_compact_decisions_recover_exactly(tmp_path,monkeypatch,local):
    history=['anchor-a']+[f'historic-{i:06d}' for i in range(1600)]
    reference,ids=seeded()
    reference.people.rows['u','alice']['faceIds']=json.dumps(history)
    expected=comparable(run_single_pass_reference(reference,ids))
    h,snapshots,store,frozen,leases=setup()
    h.people.rows['u','alice']['faceIds']=json.dumps(history)
    h.assigner.config=replace(h.assigner.config,work_dir=str(tmp_path/'work'),
                             checkpoint_dir=str(tmp_path/'mount'))
    (tmp_path/'work').mkdir()
    with pytest.raises(wave.AssignmentYield):
        run(h,snapshots,store,frozen,leases,local=local,stop=True)
    # Exercise canonical journal fallback, including reconstruction of the
    # ORIGINAL baseline whitespace, not merely the optional native snapshot.
    monkeypatch.setattr(resume,'restore',lambda *a,**kw:None)
    run(h,snapshots,store,frozen,leases,local=local,page_size=3)
    assert comparable(assignment_snapshot(h)) == expected
    if local:
        from upload_waves import digest
        path=tmp_path/'mount'/'wave-journals'/digest([store.library,frozen['waveId']])/'journal.jsonl'
        records=[json.loads(line) for line in path.read_text().splitlines()]
        decisions=[r['payload'] for r in records if r['key'].startswith('decision!')]
        assert any(r['key'].startswith('personmembers!') for r in records)
    else:
        journal=wave.WaveAssignmentJournal(store,frozen['waveId'],lambda:None)
        decisions=[journal.decision(i) for i in range(len(snapshots))]
        assert list(journal.rows('personmembers'))
    assigned=[r for r in decisions if r['outcome']=='assigned']
    assert assigned and all(r['membershipMode']==wave.MEMBERSHIP_DELTA for r in assigned)
    assert all('faceIds' not in r['person'] for r in assigned)
    assert max(len(wave.encoded(r).encode('utf-16-le')) for r in assigned) < 5000


@pytest.mark.parametrize('unicode',[False,True])
def test_snapshot_parts_preserve_text_and_reject_missing_or_rebound_parts(monkeypatch,unicode):
    h,_,store,frozen,_=setup()
    membership=json.dumps([str(i)+'😀'*6 for i in range(1400)],ensure_ascii=False) if unicode else json.dumps(
        ['anchor-a']+[f'historic-{i:06d}' for i in range(1600)])
    person=dict(PartitionKey='u',RowKey='alice',faceIds=membership,repEmbedding='[1,0,0]')
    assert len(membership.encode('utf-16-le')) <= 65536
    assert len(wave.encoded(person).encode('utf-16-le')) > wave.JOURNAL_PAYLOAD_BYTES
    journal=wave.WaveAssignmentJournal(store,frozen['waveId'],lambda:None)
    rows={}
    def put(key,value):
        assert len(wave.encoded(value).encode('utf-16-le')) <= wave.JOURNAL_PAYLOAD_BYTES
        rows[key]=deepcopy(value)
    monkeypatch.setattr(journal,'put',put)
    monkeypatch.setattr(journal,'get',rows.get)
    binding={'algorithm':wave.COMPACT_ALGORITHM}
    journal._freeze_person(person,binding)
    header=next(value for key,value in rows.items() if key.startswith('person!'))
    assert journal._restore_person(header,binding)==person
    key=next(key for key in rows if key.startswith('personmembers!'))
    saved=rows.pop(key)
    with pytest.raises(ValueError,match='membership part'):
        journal._restore_person(header,binding)
    rows[key]=dict(saved,data=saved['data']+'x')
    with pytest.raises(ValueError,match='membership (part|digest)'):
        journal._restore_person(header,binding)


def test_existing_legacy_wave_keeps_full_decisions_and_digest_on_resume(monkeypatch):
    expected=oracle()
    h,snapshots,store,frozen,leases=setup()
    with monkeypatch.context() as old:
        old.setattr(wave,'COMPACT_ALGORITHM',wave.LEGACY_ALGORITHM)
        old.setattr(wave,'_compact_decision',lambda decision:decision)
        with pytest.raises(wave.AssignmentYield):
            run(h,snapshots,store,frozen,leases,stop=True)
    journal=wave.WaveAssignmentJournal(store,frozen['waveId'],lambda:None)
    first=deepcopy(journal.decision(0))
    monkeypatch.setattr(wave,'_compact_decision',lambda *a:pytest.fail('legacy wave upgraded'))
    run(h,snapshots,store,frozen,leases,page_size=3)
    assert journal.decision(0)==first
    assert journal.get('manifest')['binding']['algorithm']==wave.LEGACY_ALGORITHM
    assert comparable(assignment_snapshot(h))==expected
    assert all('membershipMode' not in journal.decision(i) for i in range(len(snapshots)))


def test_compact_wave_refuses_a_legacy_worker_binding_before_source_effects(monkeypatch):
    h,snapshots,store,frozen,leases=setup()
    with pytest.raises(wave.AssignmentYield):
        run(h,snapshots,store,frozen,leases,stop=True)
    before=comparable(assignment_snapshot(h))
    monkeypatch.setattr(wave,'COMPACT_ALGORITHM',wave.LEGACY_ALGORITHM)
    with pytest.raises(ValueError,match='manifest or matcher policy changed'):
        run(h,snapshots,store,frozen,leases)
    assert comparable(assignment_snapshot(h))==before


@pytest.mark.parametrize('mode,person',[
    ('future-membership-mode',{}),
    (wave.MEMBERSHIP_DELTA,{'faceIds':'[]'}),
])
def test_invalid_membership_encoding_refuses_materialization(mode,person):
    h,_,_,_,_=setup()
    before=comparable(assignment_snapshot(h))
    decision=dict(outcome='assigned',membershipMode=mode,person=person)
    with pytest.raises(ValueError,match='membership (encoding|snapshot)'):
        wave._apply(h.assigner,'u',{},decision,'decision',None,'wave')
    assert comparable(assignment_snapshot(h))==before
