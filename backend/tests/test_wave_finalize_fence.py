"""Use the real finalize function and native ETag fake, including cleanup races."""
import pytest
from azure.core.exceptions import ResourceModifiedError
from test_raw_finalize_fast_path import finalize_ctx
from test_wave_recovery import RuntimeTable
import storage_utils
import wave_policy


@pytest.mark.parametrize('race',[False,True])
def test_live_finalize_is_conditional_and_cannot_resurrect_cleanup_tombstone(finalize_ctx,monkeypatch,race):
    monkeypatch.setitem(storage_utils._CTX,'wave_owned',None)
    monkeypatch.setattr(storage_utils,'touch_user_search_indexes_state',lambda *a,**kw:None)
    table=RuntimeTable()
    table.create_entity(dict(PartitionKey='lib-A',RowKey='photo.cr3',anonymousImageId='p'))
    monkeypatch.setitem(storage_utils._CTX,'metadata_table_client',table)
    # Keep native table semantics while ignoring real SDK transport options.
    original=table.update_entity
    calls=[]
    def update(row,**kwargs):
        calls.append(kwargs)
        kwargs.pop('timeout');kwargs.pop('retry_total')
        return original(row,**kwargs)
    monkeypatch.setattr(table,'update_entity',update)
    checks=[]
    def gate():
        checks.append(1)
        if len(checks)==2 and race:
            current=table.get_entity('lib-A','photo.cr3')
            original(dict(current,processing_state='deleted'),etag=current.metadata['etag'],match_condition='IfNotModified')
    token=wave_policy.begin_finalization(gate)
    try:
        if race:
            with pytest.raises(ResourceModifiedError):
                storage_utils.finalize_uploaded_file('lib-A','photo.cr3','image/x-canon-cr3',client_sha256='a'*64,anonymous_blob_name='p')
            assert table.get_entity('lib-A','photo.cr3')['processing_state']=='deleted'
        else:
            storage_utils.finalize_uploaded_file('lib-A','photo.cr3','image/x-canon-cr3',client_sha256='a'*64,anonymous_blob_name='p')
            assert table.get_entity('lib-A','photo.cr3')['fileHash']=='a'*64
        assert calls[0]['etag']
    finally:wave_policy.end_finalization(token)


@pytest.mark.parametrize('batch',[False,True])
def test_live_init_cannot_overwrite_a_concurrent_deletion(finalize_ctx,monkeypatch,batch):
    table=RuntimeTable();table.create_entity(dict(PartitionKey='lib-A',RowKey='photo.cr3',anonymousImageId='p'))
    monkeypatch.setitem(storage_utils._CTX,'metadata_table_client',table)
    native=table.submit_transaction
    def submit(operations,**transport):return native(operations)
    monkeypatch.setattr(table,'submit_transaction',submit)
    checks=[]
    def gate():
        checks.append(1)
        if len(checks)==2:
            row=table.get_entity('lib-A','photo.cr3')
            native([('update',dict(row,processing_state='deleted'),dict(etag=row.metadata['etag'],match_condition='IfNotModified'))])
    token=wave_policy.begin_finalization(gate)
    try:
        with pytest.raises(ResourceModifiedError):
            if batch:
                storage_utils.reset_upload_tracking_and_reserve_blobs_batch('lib-A',[dict(filename='photo.cr3',total_size=10,expected_hash='a'*64,is_fresh=True)])
            else:
                storage_utils.reset_upload_tracking_and_reserve_blob('lib-A','photo.cr3',10,'a'*64)
        assert table.get_entity('lib-A','photo.cr3')['processing_state']=='deleted'
    finally:wave_policy.end_finalization(token)
