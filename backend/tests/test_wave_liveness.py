"""Real wave/dispatch/lease regressions for large uploads and continuations."""
import json
import sys
import time
import threading
from collections import Counter
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from execution_lease import ExecutionLeases
from processing_completion import STEPS, prepare_completion
from processing_generation import REQUIRED_STEPS
from processing_receipts import ArtifactReceipts
from test_upload_waves import close, finish_scan
from test_wave_execution_consumers import FakeMetadata, settled_row
from test_wave_recovery import RuntimeTable
from upload_wave_execution import dispatch_outbox, run_vision_release, stage_work_store
from upload_waves import ShadowWaves, digest
from wave_publication import ObligationLedger, compute_obligations
import wave_policy
import wave_runtime


@pytest.fixture
def live(monkeypatch):
    for key,value in dict(UPLOAD_WAVE_EXECUTION_ENABLED='true',PROCESSING_MODE='backend',
            UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED='true',PROCESSING_GENERATION_ENFORCEMENT_ENABLED='true',
            UPLOAD_WAVE_DEPLOYMENT_ID='build',APP_ROLE='vision').items():monkeypatch.setenv(key,value)
    table=RuntimeTable();store=ShadowWaves(table,'library-A');metadata=FakeMetadata();sent=[];tags=[]
    certificate=wave_policy.certify(table,{role:'build' for role in wave_policy.ROLES})
    monkeypatch.setenv('UPLOAD_WAVE_CAPABILITY_ID',certificate)
    table.create_entity(store._row('policy',protocol=wave_policy.PROTOCOL,capabilityId=certificate,cleanupState=''))
    app=SimpleNamespace(jobs_table_client=table,metadata_table_client=metadata,
        blob_service_client=None,BLOB_LEXICAL_INDEX_CONTAINER='indexes',
        IPWORKER_CONCURRENCY=2,_ipwork_metrics_context=threading.local(),
        reconcile_face_filename_indexes_batch=lambda *a,**kw:None,
        _live_faiss_metadata_update=lambda *a:None,
        _get_live_faiss_assigner=lambda:pytest.fail('zero-face work loaded FAISS'))
    monkeypatch.setitem(sys.modules,'app',app)
    monkeypatch.setattr(wave_runtime,'send_stage',lambda *a:sent.append(a))
    monkeypatch.setattr(wave_runtime,'invalidate_wave_tags',lambda storage,library:tags.append((library,)))
    return store,metadata,sent,tags


def admit(store,metadata,count,session='s'):
    opened=store.open_session(session,count)
    for start in range(0,count,20):
        store.register(session,[dict(itemId=f'i{i}',filename=f'{session}-{i}.jpg') for i in range(start,min(start+20,count))])
    for i in range(count):
        filename=f'{session}-{i}.jpg';photo=f'{session}-p{i}';revision=f'{session}-r{i}'
        opened=store.resolve(session,f'i{i}',outcome='accepted',filename=filename,
            photo_id=photo,processing_revision=revision,required_steps=REQUIRED_STEPS)
        metadata.rows[store.library,filename]=dict(settled_row(photo,revision,filename=filename),face_status='pending')
    close(store,session,opened)
    return opened['waveId']


def complete_source(store,metadata,filename):
    row=metadata.rows[store.library,filename]
    row.update({step+'_status':'no_data' for step in STEPS})
    receipts=ArtifactReceipts(store.table,store.library)
    body=dict(schemaVersion=1,library=store.library,filename=filename,photoId=row['anonymousImageId'],
        revision=row['processingRevision'],statuses={step:row[step+'_status'] for step in STEPS},
        derivatives={},faceCount=0,faceSnapshots=[],faceLookupGeneration='lookup')
    row['artifactReceipt']=json.dumps(dict(id=receipts._put('manifest',body),library=store.library,
        filename=filename,photoId=row['anonymousImageId'],revision=row['processingRevision']))
    prepare_completion(row)
    intent=json.loads(row['processingCompletionOutbox'])[-1];store.accept_source_completion(intent)
    return intent


def wake(store,wave,stage):
    row=store.status(wave)
    key=row.get('visionPendingKey') if stage=='vision' else 'shadow-cluster!'+wave if stage=='cluster' else row['indexPendingKey']
    work=stage_work_store(store.table);dispatch_outbox(work,store,store.library,key,stage)
    scope=f'wave-{stage}:{wave}:{digest(key)}';pending=work._get(work.key(store.library,scope))
    return dict(userId=store.library,waveId=wave,stage=stage,scope=scope,intentRevision=pending['revision'])


def test_expired_vision_budget_releases_ownership_and_sends_continuation(live):
    store,metadata,sent,_=live;wave=admit(store,metadata,1);payload=wake(store,wave,'vision')
    assert wave_runtime.consume(payload,deadline=time.monotonic()-1)=='done'
    lease=store._get(ExecutionLeases.key(store.library,wave,'vision'))
    assert not lease['owner'] and lease['leaseUntil']==0
    pending=stage_work_store(store.table)._get(stage_work_store(store.table).key(store.library,payload['scope']))
    assert pending['attempts']==0 and len(sent)==1 and sent[0][3]>0
    complete_source(store,metadata,'s-0.jpg')
    wave_runtime.consume(payload)
    assert store.status(wave)['state']=='indexing'


def test_tag_invalidation_failure_does_not_block_wave_completion(live,monkeypatch):
    """A tag-embedding-index write failure (e.g. the blob container not
    existing yet) must never abort the whole wave-vision completion -- it's a
    minor, independently-recoverable staleness issue, not a reason to block
    every upload's gallery visibility. Reproduces the real production
    incident: invalidate_wave_tags raised, uncaught, out of before_freeze."""
    import wave_runtime
    def boom(storage,library):
        raise RuntimeError('Wave tag invalidation was not durably published')
    monkeypatch.setattr(wave_runtime,'invalidate_wave_tags',boom)
    store,metadata,sent,_=live;wave=admit(store,metadata,1)
    complete_source(store,metadata,'s-0.jpg')
    payload=wake(store,wave,'vision')
    assert wave_runtime.consume(payload)=='done'
    assert store.status(wave)['state']=='indexing'


def test_resume_skips_processed_prefix_but_revisits_unresolved_sources(live,monkeypatch):
    store,metadata,sent,_=live;wave=admit(store,metadata,3);calls=[];now=[0.0]
    monkeypatch.setattr(time,'monotonic',lambda:now[0])
    def process(library,filename,revision,renew):
        calls.append(filename);complete_source(store,metadata,filename);now[0]+=6
    leases=ExecutionLeases(store.table,partition=store.partition);work=stage_work_store(store.table)
    key=store.status(wave)['visionPendingKey']
    for i in range(3):
        result=run_vision_release(leases,work,store,store.library,wave,key,metadata,None,None,
            lambda *a:sent.append(a),process_source=process,deadline=now[0]+5)
    assert len(calls)==len(set(calls))==3
    run_vision_release(leases,work,store,store.library,wave,key,metadata,None,None,
        lambda *a:sent.append(a),process_source=process,deadline=now[0]+5)
    assert store.status(wave)['state']=='indexing'


def test_evaluation_yields_and_resumes_without_reprocessing_sources(live,monkeypatch):
    store,metadata,sent,_=live;wave=admit(store,metadata,25)
    for filename in [name for library,name in metadata.rows]:complete_source(store,metadata,filename)
    now=[0.0];monkeypatch.setattr(time,'monotonic',lambda:now[0]);original=store.evaluate
    def slow(*args,**kw):
        row=original(*args,**kw);now[0]+=6;return row
    monkeypatch.setattr(store,'evaluate',slow)
    leases=ExecutionLeases(store.table,partition=store.partition);work=stage_work_store(store.table);key=store.status(wave)['visionPendingKey']
    result=run_vision_release(leases,work,store,store.library,wave,key,metadata,None,None,lambda *a:None,deadline=5)
    assert result['evaluation'] and result['state']!='ready'
    import upload_wave_execution
    monkeypatch.setattr(upload_wave_execution,'_observe_qualified',lambda *a:pytest.fail('revisited source during evaluation'))
    run_vision_release(leases,work,store,store.library,wave,key,metadata,None,None,lambda *a:sent.append(a),deadline=100)
    assert store.status(wave)['state']=='ready' and sent


def test_twenty_zero_face_members_continue_on_cluster_queue_without_faiss(live,monkeypatch):
    store,metadata,sent,tags=live;wave=admit(store,metadata,20)
    for library,filename in list(metadata.rows):complete_source(store,metadata,filename)
    wave_runtime.consume(wake(store,wave,'vision'))
    assert store.status(wave)['zeroFaceProbePending'] and sent[-1][1].startswith('wave-cluster:')
    assert tags==[(store.library,)]
    monkeypatch.setenv('APP_ROLE','cluster');wave_runtime.consume(wake(store,wave,'cluster'))
    assert store.status(wave)['state']=='indexing' and sent[-1][1].startswith('wave-index:')


def test_stopped_selection_processes_accepted_prefix_through_publication(live,monkeypatch):
    store,metadata,sent,_=live
    opened=store.open_session('stopped',25)
    for start in (0,20):
        store.register('stopped',[dict(itemId=str(i),filename=f'{i}.jpg') for i in range(start,min(start+20,25))])
    for i in range(25):
        if i<20:
            row=store.resolve('stopped',str(i),outcome='accepted',filename=f'{i}.jpg',
                photo_id=f'p{i}',processing_revision=f'r{i}',required_steps=REQUIRED_STEPS)
            metadata.rows[store.library,f'{i}.jpg']=settled_row(f'p{i}',f'r{i}',filename=f'{i}.jpg')
        else:
            row=store.resolve('stopped',str(i),outcome='cancelled')
    close(store,'stopped',row)
    wave=opened['waveId']
    assert store.status(wave)['memberCount']==20
    for i in range(20):complete_source(store,metadata,f'{i}.jpg')
    wave_runtime.consume(wake(store,wave,'vision'))
    monkeypatch.setenv('APP_ROLE','cluster')
    wave_runtime.consume(wake(store,wave,'cluster'))
    assert store.status(wave)['state']=='indexing'
    import index_publication,wave_catalog,index_events
    monkeypatch.setattr(index_publication,'catalog_serialization',lambda *a:nullcontext())
    monkeypatch.setattr(wave_catalog,'CatalogPublisher',lambda *a:SimpleNamespace(publish=lambda *a:'generation'))
    monkeypatch.setattr(index_events,'publish',lambda *a:None)
    monkeypatch.setenv('APP_ROLE','index-worker')
    wave_runtime.consume(wake(store,wave,'index'))
    assert store.status(wave)['state']=='complete'
    assert store.status(wave)['memberCount']==20


def test_two_failed_sources_each_receive_their_full_retry_budget(live,monkeypatch):
    store,metadata,sent,_=live;wave=admit(store,metadata,2);payload=wake(store,wave,'vision');calls=Counter()
    def failed(library,filename,revision,renew):
        calls[filename]+=1;metadata.rows[library,filename]['face_status']='failed'
        raise RuntimeError('recorded inference failure')
    monkeypatch.setattr(wave_runtime,'process_source',failed)
    wave_runtime.consume(payload)
    assert sorted(calls.values())==[1,1]
    assert store.status(wave)['state']=='indexing' and store.status(wave)['failedMembers']==2


def test_source_with_no_required_steps_exhausts_without_blocking_its_wave_mate(live,monkeypatch):
    """Browser-owned video has an empty requiredSteps contract, so a bare
    exception from process_source -- the production incident: an uncaught
    crash in completion/receipt capture, unrelated to any step's own status
    -- has no required step that could ever be recorded 'failed'. Before this
    fix exhausted() could never fire for such a source no matter the attempt
    count, and the uncaught exception aborted the whole pass, starving every
    other member in the same page too. A healthy sibling must still resolve
    on the very first wake, and the broken source must still give up after
    its full retry budget instead of retrying forever."""
    store,metadata,sent,_=live
    opened=store.open_session('mixed',2)
    store.register('mixed',[dict(itemId='0',filename='poison.mp4'),dict(itemId='1',filename='healthy.jpg')])
    store.resolve('mixed','0',outcome='accepted',filename='poison.mp4',
        photo_id='p-poison',processing_revision='r-poison',required_steps=[])
    resolved=store.resolve('mixed','1',outcome='accepted',filename='healthy.jpg',
        photo_id='p-healthy',processing_revision='r-healthy',required_steps=REQUIRED_STEPS)
    metadata.rows[store.library,'poison.mp4']=settled_row('p-poison','r-poison',filename='poison.mp4')
    metadata.rows[store.library,'healthy.jpg']=settled_row('p-healthy','r-healthy',filename='healthy.jpg')
    close(store,'mixed',resolved)
    wave=resolved['waveId'];payload=wake(store,wave,'vision');calls=Counter()
    def process(library,filename,revision,renew):
        calls[filename]+=1
        if filename=='poison.mp4':
            raise ValueError('Skipped face work with existing faces requires an explicit prior receipt')
        complete_source(store,metadata,filename)
    monkeypatch.setattr(wave_runtime,'process_source',process)
    wave_runtime.consume(payload)
    assert calls=={'poison.mp4':1,'healthy.jpg':1}
    assert store.status(wave)['state']=='indexing'
    assert store.status(wave)['failedMembers']==1


def test_index_completion_wakes_a_waiting_successor(live,monkeypatch):
    store,metadata,sent,_=live;first=admit(store,metadata,1)
    intent=complete_source(store,metadata,'s-0.jpg')
    store.observe(first,'s-p0','s-r0',outcome='settled',evidence_id=intent['id'],evidence_qualified=True)
    frozen=finish_scan(store,first)
    ledger=ObligationLedger(store.table,first,library=store.library,frozen_revision=frozen['frozenRevision'])
    ids=compute_obligations(store.library,first,frozen['frozenRevision'],['sort'])
    frontier=ledger.record_required(ids);store.commit_assignment(first,frontier,manifest_digest=frozen['manifestDigest'])
    second=admit(store,metadata,1,session='second')
    assert store.status(second)['state']=='waiting_predecessor'
    import index_publication,wave_catalog,index_events
    monkeypatch.setattr(index_publication,'catalog_serialization',lambda *a:nullcontext())
    monkeypatch.setattr(wave_catalog,'CatalogPublisher',lambda *a:SimpleNamespace(publish=lambda *a:'generation'))
    monkeypatch.setattr(index_events,'publish',lambda *a:None)
    monkeypatch.setenv('APP_ROLE','index-worker');wave_runtime.consume(wake(store,first,'index'))
    assert store.status(first)['state']=='complete'
    assert store.status(second)['state']=='processing_pending'
    assert any(f'wave-vision:{second}:' in message[1] for message in sent)


def test_runtime_restores_face_preparation_batches_and_source_metrics(live, monkeypatch):
    import threading
    from execution_lease import guard_operations
    store, metadata, sent, _ = live
    wave = admit(store, metadata, 25)
    app = sys.modules['app']
    marker = object()
    app._ipwork_metrics_context.collector = marker
    prepared, processed = [], []
    lock = threading.Lock()
    def prepare(library, filenames, cancelled):
        assert library == store.library and not cancelled()
        prepared.append(list(filenames))
    app.reconcile_face_filename_indexes_batch = prepare
    def process(library, filename, revision, renew):
        assert app._ipwork_metrics_context.collector is marker
        assert guard_operations(store)
        complete_source(store, metadata, filename)
        with lock:
            processed.append(filename)
    monkeypatch.setattr(wave_runtime, 'process_source', process)
    assert wave_runtime.consume(wake(store, wave, 'vision')) == 'done'
    assert [len(names) for names in prepared] == [20, 5]
    assert len(processed) == len(set(processed)) == 25
    assert set(processed) == {name for names in prepared for name in names}
