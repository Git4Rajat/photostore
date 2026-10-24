import pytest
from upload_waves import ShadowWaves
from wave_recovery import recover,DIRECTORY
from test_upload_waves import AtomicTable,close


def test_disabled_recovery_makes_no_storage_calls(monkeypatch):
    monkeypatch.delenv('UPLOAD_WAVE_RECOVERY_ENABLED',raising=False)
    assert recover(object(),lambda *a:pytest.fail('send'))['disabled']


def test_independent_scan_recovers_intake_close_handoff_and_is_bounded(monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    monkeypatch.setenv('UPLOAD_WAVE_RECOVERY_ENABLED','true')
    table=RuntimeTable();waves=[]
    for name in ('a','b','c'):
        store=ShadowWaves(table,name)
        opened=store.open_session('s',0);close(store,'s',opened)
        waves.append(store)
    sent=[];observed=[]
    # Native WorkStore methods need the fake's atomic adapters too.
    from test_index_work import EtagTable
    for attr in ('create_entity','update_entity','delete_entity'):
        def operation(*args,_attr=attr,**kwargs):
            if _attr=='create_entity':return table.submit_transaction([('create',args[0])])
            if _attr=='update_entity':return table.submit_transaction([('update',args[0],kwargs)])
            key=(args[0],args[1]);del table.rows[key]
        setattr(table,attr,operation)
    seen=set()
    for _ in range(3):
        result=recover(table,lambda *a:sent.append(a),max_libraries=1,wave_pages=1,
            emit=lambda **data:observed.append(data))
        assert result['libraries']==1
        seen.add(sent[-1][0])
    assert seen=={'a','b','c'}
    assert all(record['handoff']=='vision' for record in observed)
    assert table.get_entity(DIRECTORY,'scan-cursor')['after']


class RuntimeTable(AtomicTable):
    def create_entity(self,row):return self.submit_transaction([('create',row)])
    def update_entity(self,row,**options):
        from azure.data.tables import UpdateMode
        if options.get('mode')==UpdateMode.MERGE:
            row=dict(self.get_entity(row['PartitionKey'],row['RowKey']),**row)
        return self.submit_transaction([('update',row,options)])
    def delete_entity(self,partition,row,**options):
        current=self.get_entity(partition,row)
        if options.get('etag'):assert current.metadata['etag']==options['etag']
        del self.rows[partition,row]
    def query_entities(self,query,**options):
        if 'RowKey ge' in query:
            return super().query_entities(query,**options)
        from test_index_work import EtagTable
        return EtagTable.query_entities(self,query,**options)


def test_poison_library_does_not_starve_next_library(monkeypatch):
    import wave_recovery
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED','true')
    monkeypatch.setenv('UPLOAD_WAVE_RECOVERY_ENABLED','true')
    table=RuntimeTable()
    for name in ('a','b'):
        store=ShadowWaves(table,name);opened=store.open_session('s',0);close(store,'s',opened)
    original=wave_recovery.advance_wave
    def broken(work,store,*args):
        if store.library=='a':raise RuntimeError('poison wave')
        return original(work,store,*args)
    monkeypatch.setattr(wave_recovery,'advance_wave',broken)
    sent=[];events=[]
    result=recover(table,lambda *args:sent.append(args),emit=lambda **fields:events.append(fields))
    assert result['errors']==1 and result['libraries']==2
    assert any(args[0]=='b' for args in sent)
    assert any(row['event']=='wave_recovery_error' for row in events)


def test_live_stage_lease_is_not_reported_as_an_unsent_handoff():
    from execution_lease import ExecutionLeases
    from wave_recovery import metrics
    table=RuntimeTable();store=ShadowWaves(table,'a')
    opened=store.open_session('s',0);close(store,'s',opened)
    wave=store._required(store.wave_key(opened['waveId']))
    before=metrics(store,wave)
    assert before['handoff_pending_count']==1
    leases=ExecutionLeases(table,partition=store.partition)
    leases.claim('a',wave['waveId'],'vision','revision')
    after=metrics(store,wave)
    assert after['handoff_pending_count']==0 and after['stage_lease_active']
