"""Historical failures stay diagnosable without retaining payloads or URLs."""
import json

import pytest

from processing_failures import capsule, source_details, exception_details
from wave_settlement import settle, verified
from test_wave_activation import active, close, RuntimeTable


def test_capsule_is_bounded_and_discards_private_witness_fields():
    value = capsule(dict(attempts=4, generation='generation-1', statuses={'ocr':'failed', 'filename':'secret.jpg'},
        errors={'ocr':'ocr_timeout', 'face':'https://signed-url'}, error='https://private',
        filename='private.jpg', message='private', data='x' * 100000))
    assert value == dict(schemaVersion=1, stage='vision', attempts=4, generation='generation-1',
                         statuses={'ocr':'failed'}, errors={'ocr':'ocr_timeout'})
    assert source_details({'processing_metadata':'[]'})['errors'] == {}
    assert source_details({'processing_metadata':'broken'})['errors'] == {}


def test_exception_capsule_retains_class_and_status_without_message():
    error = RuntimeError('private.jpg?sig=secret')
    error.status_code = 503
    error.__cause__ = TimeoutError('signed-url')
    assert exception_details(error) == dict(errorClass='RuntimeError', httpStatus=503, causeClass='TimeoutError')


def test_failure_proof_retains_redacted_witness_after_source_disappears():
    store = active(RuntimeTable())
    session=store.open_session('s',1);store.register('s',[dict(itemId='i',filename='a.jpg')])
    session=store.resolve('s','i',outcome='accepted',photo_id='p',processing_revision='r',filename='a.jpg',required_steps=['ocr'])
    close(store,'s',session)
    member=store._page(f'e!{session["waveId"]}!m!')[0]
    member=settle(store,session['waveId'],member,'failed_exhausted','required_step_retries_exhausted',
                  witness=dict(attempts=1, statuses={'ocr':'failed'}, errors={'ocr':'ocr_timeout'}, message='private'))
    proof=json.loads(store._get('settlement!'+member['evidenceId'])['payload'])
    assert proof['failure']['errors'] == {'ocr':'ocr_timeout'}
    assert 'private' not in json.dumps(proof)
    assert verified(store,member)


@pytest.mark.parametrize('stage', ['vision','cluster','index'])
def test_dispatch_capsule_is_immutable_and_owner_fenced(stage):
    from upload_wave_execution import stage_work_store
    # RuntimeTable supports genuine ETags and atomic multi-row transactions.
    work=stage_work_store(RuntimeTable())
    key=work.request('lib', f'wave-{stage}:wave:outbox')
    claim=work.claim(key)
    failure_id=work.record_failure(claim,dict(errorClass='TimeoutError', attempts=claim.attempt))
    first=dict(work._get(failure_id))
    assert json.loads(first['payload'])['stage'] == stage
    assert work.record_failure(claim,dict(errorClass='DifferentError')) == failure_id
    assert dict(work._get(failure_id)) == first
    work.release(claim)
    with pytest.raises(RuntimeError,match='lease lost'):
        work.record_failure(claim,dict(errorClass='OldOwner'))
    assert work.claim(failure_id) is None


def test_checkpoint_retains_index_phase_but_hashes_member_cursor():
    from processing_failures import checkpoint_details
    details=checkpoint_details('index:'+json.dumps(dict(phase='members',after='private-photo-key')))
    assert details['phase']=='members'
    assert len(details['cursorDigest'])==64
    assert 'private' not in json.dumps(details)


@pytest.mark.parametrize('source_state', ['matching_generation','different_generation','missing','deleted'])
def test_failure_review_uses_exact_generation_and_never_repairs(source_state):
    from test_upload_waves import register_accepted
    from wave_failure_review import page
    store=active(RuntimeTable());wid,opened=register_accepted(store)
    close(store,'parent',opened)
    member=store._page(f'e!{wid}!m!')[0]
    settle(store,wid,member,'failed_exhausted','source_retries_exhausted',witness=dict(attempts=1))
    metadata=RuntimeTable()
    if source_state!='missing':
        metadata.create_entity(dict(PartitionKey=store.library,RowKey=member['filename'],
            anonymousImageId=member['photoId'],processingRevision='new' if source_state=='different_generation' else member['processingRevision'],
            processing_state='deleted' if source_state=='deleted' else 'active',ocr_status='failed',
            processing_metadata=json.dumps(dict(client_ocr=dict(errorCode='ocr_timeout')))))
    before=repr((store.table.rows,metadata.rows))
    result=page(store,metadata,wid)
    failure,=result['failures']
    assert failure['disposition']==source_state
    assert ('currentFailure' in failure)==(source_state=='matching_generation')
    assert result['readOnly'] and not result['nextAfter']
    assert repr((store.table.rows,metadata.rows))==before


def test_failure_review_rejects_foreign_and_query_injection_cursors():
    from test_upload_waves import register_accepted
    from wave_failure_review import page
    store=active(RuntimeTable());wid,opened=register_accepted(store)
    for cursor in ['other-wave', f"e!{wid}!m!' or RowKey ne '"]:
        with pytest.raises(ValueError,match='cursor'):page(store,RuntimeTable(),wid,after=cursor)


def test_error_codes_do_not_accept_filename_or_uri_shaped_labels():
    witness=dict(errors={'ocr':'private.jpg','face':'private:label','exif':'exif_parser_error'})
    assert capsule(witness)['errors']=={'exif':'exif_parser_error'}
    details=source_details(dict(ocr_status='failed',processing_metadata=json.dumps(
        dict(client_ocr=dict(errorCode='private.jpg')))))
    assert details['errors']=={}
