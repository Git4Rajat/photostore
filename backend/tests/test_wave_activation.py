import json
import pytest
from azure.core.exceptions import ResourceNotFoundError
from test_wave_recovery import RuntimeTable
from test_upload_waves import close
from upload_waves import ShadowWaves,Conflict,digest
import wave_policy as policy


def active(table,library='u'):
    store=ShadowWaves(table,library)
    table.create_entity(store._row('policy',protocol=policy.PROTOCOL,cleanupState=''))
    return store


def test_enrollment_requires_certificate_and_new_empty_library(monkeypatch):
    table=RuntimeTable();metadata=RuntimeTable()
    monkeypatch.setenv('UPLOAD_WAVE_NEW_LIBRARIES_ENABLED','true')
    monkeypatch.setenv('PROCESSING_MODE','backend')
    for name in ('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED','PROCESSING_GENERATION_ENFORCEMENT_ENABLED'):
        monkeypatch.setenv(name,'true')
    with pytest.raises(RuntimeError):policy.enroll_new(table,'u',metadata)
    certificate=policy.certify(table,{role:'build-1' for role in policy.ROLES})
    monkeypatch.setenv('UPLOAD_WAVE_CAPABILITY_ID',certificate)
    policy.enroll_new(table,'u',metadata)
    assert policy.owned(table,'u') and not policy.owned(table,'old')
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','false')
    assert policy.owned(table,'u')  # Paused execution never restores legacy ownership.
    metadata.create_entity(dict(PartitionKey='existing',RowKey='photo'))
    with pytest.raises(Conflict):policy.enroll_new(table,'existing',metadata)


@pytest.mark.parametrize('owned,source_change', [(True, True), (True, False), (False, True)])
def test_wave_source_commit_defers_legacy_dirty_manifests_but_crud_does_not(monkeypatch, owned, source_change):
    import storage_utils as storage
    touched = []
    monkeypatch.setitem(storage._CTX, 'wave_owned', lambda library: owned)
    for name in ('vector', 'lexical', 'tag_embedding', 'sort', 'access'):
        monkeypatch.setattr(storage, 'touch_user_' + name + '_index_state',
                            lambda *a, _name=name, **k: touched.append(_name))
    monkeypatch.setattr(storage, '_mark_search_index_dirty_filenames',
                        lambda *a, **k: touched.append('dirty_files'))
    monkeypatch.setattr(storage, 'sort_index_skipped', lambda library: False)
    def touch():
        storage.touch_user_search_indexes_state('u', filenames='photo.jpg')
    if source_change:
        policy.source_change(touch)()
    else:
        touch()
    assert bool(touched) is not (owned and source_change)
    if touched:
        assert 'tag_embedding' in touched and 'dirty_files' in touched


def test_reservation_cannot_change_source_or_accept_an_old_client():
    table=RuntimeTable();store=active(table)
    store.open_session('s',1);store.register('s',[dict(itemId='i',filename='a.jpg')])
    with pytest.raises(ValueError):policy.validate_admission(store,dict(filename='a.jpg',sha256='a'*64))
    data=dict(filename='a.jpg',sha256='a'*64,waveSessionId='s',waveItemId='i')
    assert policy.validate_admission(store,data)==('s','i')
    with pytest.raises(Conflict):policy.validate_admission(store,dict(data,sha256='b'*64))
    with pytest.raises(Conflict):policy.validate_admission(store,dict(data,filename='other.jpg'))


def test_cancelled_wave_completes_with_control_proof_without_any_stage(monkeypatch):
    from wave_no_work import advance_empty
    table=RuntimeTable();store=active(table)
    opened=store.open_session('s',1)
    store.register('s',[dict(itemId='i',filename='a.jpg')])
    resolved=store.resolve('s','i',outcome='cancelled');close(store,'s',resolved)
    result=advance_empty(store,opened['waveId'])
    assert result['state']=='complete' and result['completionOutcome']=='no_work'
    assert not store._page('assignment!') and not store._page('publication!')
    assert advance_empty(store,opened['waveId'])['completionReceiptId']==result['completionReceiptId']


def test_successor_can_follow_no_work_predecessor():
    from wave_no_work import advance_empty
    table=RuntimeTable();store=active(table)
    first=store.open_session('s',0);close(store,'s',first)
    second=store.open_session('t',0)
    assert second['waveId']!=first['waveId']
    assert not store.status(second['waveId']).get('predecessorFrozenRevision')
    advance_empty(store,first['waveId'])
    assert store._required('control')['collectingWaveId']==second['waveId']
    close(store,'t',second)
    assert store.status(second['waveId'])['state']=='waiting_predecessor'
    store.promote_successor(second['waveId'])
    assert advance_empty(store,second['waveId'])['state']=='complete'


def test_owner_abandonment_is_paged_and_accounts_for_unregistered_items():
    from wave_settlement import abandon
    table=RuntimeTable();store=active(table)
    store.open_session('s',23)
    for start,end in [(0,20),(20,22)]:
        store.register('s',[dict(itemId=f'i{i:02}',filename=f'{i}.jpg') for i in range(start,end)])
    session=store._required(store.session_key('s'))
    with pytest.raises(Conflict):abandon(store,'s','stale',reason='upload stopped')
    first=abandon(store,'s',session['manifestRevision'],reason='upload stopped')
    assert first['abandonmentNeedsMore']
    with pytest.raises(Conflict):policy.validate_admission(store,dict(waveSessionId='s',waveItemId='i21',filename='21.jpg',sha256='a'*64))
    second=abandon(store,'s',first['manifestRevision'],reason='upload stopped')
    assert second['state']=='abandoned' and second['resolvedCount']==22
    assert second['declaredCount']==23


def test_missing_source_requires_an_explicit_witness():
    from wave_settlement import source_state
    table=RuntimeTable();store=active(table);metadata=RuntimeTable()
    session=store.open_session('s',1);store.register('s',[dict(itemId='i',filename='a.jpg')])
    session=store.resolve('s','i',outcome='accepted',photo_id='p',processing_revision='r',filename='a.jpg',required_steps=['face'])
    close(store,'s',session)
    member=store._page(f'e!{session["waveId"]}!m!')[0]
    assert source_state(store,session['waveId'],member,metadata) is None


def test_settlement_has_distinct_failure_proof_and_cannot_mutate_frozen_input():
    from wave_settlement import settle,verified
    from test_upload_waves import finish_scan
    table=RuntimeTable();store=active(table)
    session=store.open_session('s',1);store.register('s',[dict(itemId='i',filename='a.jpg')])
    session=store.resolve('s','i',outcome='accepted',photo_id='p',processing_revision='r',filename='a.jpg',required_steps=['face'])
    close(store,'s',session);member=store._page(f'e!{session["waveId"]}!m!')[0]
    member=settle(store,session['waveId'],member,'failed_exhausted','test_failure',witness={'attempts':5})
    assert verified(store,member)
    assert not store._page('source-completion!')
    assert finish_scan(store,session['waveId'])['failedMembers']==1
    with pytest.raises(Conflict):settle(store,session['waveId'],member,'deleted','changed',witness={})


def execution_world(monkeypatch):
    from execution_lease import ExecutionLeases
    from upload_wave_execution import stage_work_store
    from test_index_work import EtagTable
    table=RuntimeTable();store=ShadowWaves(table,'library-A')
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    return store,stage_work_store(EtagTable()),ExecutionLeases(table,partition=store.partition),table


def test_zero_face_assignment_uses_control_proof_then_duplicate_wave_skips_all_stages(monkeypatch):
    from test_wave_execution_consumers import ready_wave
    from wave_no_work import advance_zero_faces,advance_empty
    from wave_publication import ObligationLedger
    from upload_wave_execution import run_index_release
    world=execution_world(monkeypatch);store,work,leases,table=world
    wave_id=ready_wave(world)
    active(table,store.library)
    indexed=advance_zero_faces(store,wave_id)
    assert indexed['state']=='indexing'
    assignment=json.loads(store._get(f'assignment!{wave_id}!complete')['payload'])
    assert assignment['outcome']=='no_work'
    ledger=ObligationLedger(table,wave_id,library=store.library,frozen_revision=indexed['frozenRevision'],
        assignment_revision=indexed['assignmentReceiptId'])
    completed=run_index_release(leases,store,store.library,wave_id,indexed['indexPendingKey'],ledger,
        publisher=lambda scope,obligation,check:digest([scope,obligation]))
    assert completed['state']=='complete'
    original=store._page(f'e!{wave_id}!m!')[0]
    session=store.open_session('duplicate',1)
    store.register('duplicate',[dict(itemId='i',filename='original.jpg')])
    session=store.resolve('duplicate','i',outcome='duplicate',filename='original.jpg',
        photo_id=original['photoId'],processing_revision=original['processingRevision'],required_steps=json.loads(original['requiredSteps']))
    close(store,'duplicate',session);store.promote_successor(session['waveId'])
    result=advance_empty(store,session['waveId'])
    assert result['state']=='complete' and result['completionOutcome']=='no_work'
    assert not store._get('shadow-cluster!'+session['waveId'])


def test_one_failed_source_attempt_settles_as_failure_not_success(monkeypatch):
    from test_wave_execution_consumers import register_accepted,FakeMetadata,settled_row,no_faces
    from upload_wave_execution import run_vision_release,_iter_wave_faces
    world=execution_world(monkeypatch);store,work,leases,table=world
    wave_id,session=register_accepted(store);close(store,'parent',session)
    metadata=FakeMetadata();metadata.put(store.library,'original.jpg',**dict(settled_row('photo-1','revision-1'),face_status='failed'))
    result=run_vision_release(leases,work,store,store.library,wave_id,store.status(wave_id)['visionPendingKey'],
        metadata,None,no_faces,lambda *args:None,process_source=lambda *args:None)
    assert result['state']=='ready'
    assert result['failedMembers']==1
    assert list(_iter_wave_faces(store,wave_id))==[]
    assert not store._page('source-completion!')


def test_undecodable_preview_fallback_settles_on_first_attempt_without_aborting_wave(monkeypatch):
    """An undecodable RAW's preview fallback is deterministic on these bytes --
    it resolves to terminal 'unrecoverable' (not retryable 'failed') on the
    very first attempt, so the member settles via normal completion
    immediately instead of needing wave_settlement's 5-attempt exhaustion
    budget. The sibling healthy photo is unaffected either way."""
    import storage_utils
    from test_wave_execution_consumers import FakeMetadata, settled_row, no_faces, publish_completion
    from upload_wave_execution import run_vision_release
    world = execution_world(monkeypatch)
    store, work, leases, table = world
    from processing_generation import REQUIRED_STEPS
    session = store.open_session('parent', 2)
    wave_id = session['waveId']
    store.register('parent', [dict(itemId=str(i), filename=name)
        for i, name in enumerate(('original.jpg', 'healthy.jpg'))])
    for i, (filename, photo) in enumerate([('original.jpg', 'photo-1'), ('healthy.jpg', 'photo-2')]):
        session = store.resolve('parent', str(i), outcome='accepted', photo_id=photo,
            processing_revision='revision-1', filename=filename, required_steps=REQUIRED_STEPS)
    close(store, 'parent', session)
    metadata = FakeMetadata()
    metadata.put(store.library, 'original.jpg',
        **dict(settled_row('photo-1', 'revision-1'), preview_status='pending'))
    metadata.put(store.library, 'healthy.jpg', **settled_row('photo-2', 'revision-1', filename='healthy.jpg'))
    # Mirrors convert_image_to_jpeg's undecodable RAW passthrough.
    original = b'raw' * 1_437_000
    monkeypatch.setattr(storage_utils, 'convert_image_to_jpeg', lambda data, name: data)
    monkeypatch.setattr(storage_utils, '_publish_processing_derivative',
        lambda *a, **k: pytest.fail('invalid RAW bytes reached publication'))
    processed = []
    def process(library, filename, revision, renew):
        processed.append(filename)
        if filename == 'healthy.jpg':
            publish_completion(store, metadata, filename=filename)
            return
        row = metadata.rows[library, filename]
        row.update(storage_utils._apply_server_preview_fallback(library, filename, row,
            original, fallback_for='client_preview_failed'))
        assert row['preview_status'] == 'unrecoverable'
        publish_completion(store, metadata, filename=filename)
    result = run_vision_release(leases, work, store, store.library, wave_id,
        store.status(wave_id)['visionPendingKey'], metadata, None, no_faces,
        lambda *args: None, process_source=process)
    assert result['state'] == 'ready'
    members = {row['filename']: row for row in store._page(f'e!{wave_id}!m!')}
    assert members['original.jpg']['outcome'] == 'settled'
    assert members['healthy.jpg']['outcome'] == 'settled'
    assert processed.count('healthy.jpg') == 1 and processed.count('original.jpg') == 1
    assert result['failedMembers'] == 0
    assert len(store._page('source-completion!')) == 2


def test_automatic_source_wakes_suppressed_but_foreground_people_edit_still_queues(monkeypatch):
    import app
    table=RuntimeTable();store=active(table)
    store.open_session('s',1)
    monkeypatch.setattr(app,'jobs_table_client',table)
    monkeypatch.setattr(app,'index_maintenance_queue_client',object())
    monkeypatch.setattr(app,'INDEX_MAINTENANCE_QUEUE_NAME','index')
    sent=[];monkeypatch.setattr(app,'_send_index_wakeup',lambda *a,**kw:sent.append((a,kw)))
    assert app._queue_ipwork_processing('u','photo.jpg')['status']=='wave_owned'
    assert app.enqueue_index_build('u',reason='upload-end',scope='dedup')=='wave_owned'
    assert app.enqueue_index_build('u',reason='serving-process',scope='full')=='wave_owned'
    assert not sent
    assert app.enqueue_index_build('u',reason='person-label',scope='people')=='queued'
    assert len(sent)==1


def test_accepted_binding_promotes_existing_duplicate_projection_obligation():
    table=RuntimeTable();store=active(table)
    store.open_session('s',2);store.register('s',[dict(itemId=i,filename='photo.jpg') for i in ('a','b')])
    for item,outcome in [('a','duplicate'),('b','accepted')]:
        store.resolve('s',item,outcome=outcome,filename='photo.jpg',photo_id='p',processing_revision='r',required_steps=['face'])
    assert store._page('e!')[0]['needsProjection'] is True


def test_no_face_control_path_cannot_replace_running_cluster_journal(monkeypatch):
    from test_wave_execution_consumers import ready_wave
    from wave_no_work import advance_zero_faces
    from execution_lease import ExecutionLeases
    world=execution_world(monkeypatch);store,work,leases,table=world
    wave_id=ready_wave(world);active(table,store.library)
    lease=leases.claim(store.library,wave_id,'cluster',store.status(wave_id)['frozenRevision'])
    assert advance_zero_faces(store,wave_id)['state']=='ready'
    assert not store._get(f'assignment!{wave_id}!manifest')
    leases.release(lease)
    assert advance_zero_faces(store,wave_id)['state']=='indexing'


def test_supersession_recovers_after_generation_commit_before_successor_admission():
    from wave_settlement import reconcile_supersession,source_state
    from test_upload_waves import close
    table=RuntimeTable();metadata=RuntimeTable();store=active(table)
    session=store.open_session('s',1);store.register('s',[dict(itemId='i',filename='photo.jpg')])
    session=store.resolve('s','i',outcome='accepted',filename='photo.jpg',photo_id='p',processing_revision='old',required_steps=['face'])
    close(store,'s',session)
    metadata.create_entity(dict(PartitionKey='u',RowKey='photo.jpg',anonymousImageId='p',processingRevision='new',waveSupersessionBacklog='["old"]'))
    for _ in range(2):
        reconcile_supersession(store,metadata,metadata.get_entity('u','photo.jpg'))
    members=store._page(f'e!{session["waveId"]}!m!')
    assert source_state(store,session['waveId'],members[0],metadata)['outcome']=='superseded'
    successor=store._page('supersession!')[0]['successorWaveId']
    assert store.status(successor)['state']=='waiting_predecessor'
    assert store.status(successor)['memberCount']==1


def test_live_admission_reuses_healthy_lease_without_control_write():
    table=RuntimeTable();now=[1000.0]
    store=active(table);store.clock=lambda:now[0]
    store.open_session('s',1);store.register('s',[dict(itemId='i',filename='a.jpg')])
    data=dict(filename='a.jpg',sha256='a'*64,waveSessionId='s',waveItemId='i')
    policy.validate_admission(store,data,finalizing=True)
    initial=store._required('control')['revision']
    now[0]+=1
    policy.validate_admission(store,data,finalizing=True)
    assert store._required('control')['revision']==initial
    with pytest.raises(Conflict):policy.validate_admission(store,dict(data,sha256='b'*64),finalizing=True)
    now[0]+=500
    policy.validate_admission(store,data,finalizing=True)
    assert store._required('control')['revision']!=initial
    assert store._required(store.item_key('s','i'))['finalizeLeaseUntil']==now[0]+600
