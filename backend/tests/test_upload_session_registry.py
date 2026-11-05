"""Traffic-driven lifecycle: fencing races, interruption, notices and isolation."""
import pytest
from azure.core.exceptions import AzureError
from upload_waves import ShadowWaves
from upload_session_registry import Sessions, CLAIM_SECONDS
from upload_session_lease import renew
from test_upload_waves import AtomicTable
from upload_session_fakes import RegistryTable


@pytest.fixture
def world():
    now = [1000.0]
    store = ShadowWaves(AtomicTable(), 'library', clock=lambda: now[0])
    directory = Sessions(RegistryTable(), 'library', clock=lambda: now[0])
    return store, directory, now


def admit(store, directory, count=3):
    directory.track('one')
    store.open_session('one', count, lease_seconds=300, lifecycle_managed=True)
    store.register('one', [dict(itemId='a', filename='a.jpg'), dict(itemId='b', filename='b.jpg')])
    store.resolve('one', 'a', outcome='accepted', filename='a.jpg', photo_id='photo',
                  processing_revision='r', required_steps=['face'])


def test_stale_closure_preserves_completed_files_and_counts_unregistered_files(world):
    store, directory, now = world
    admit(store, directory)
    now[0] += 301
    driven = []
    assert not directory.reconcile(store, driven.append)
    parent = store._required(store.session_key('one'))
    assert parent['state'] == 'abandoned'
    assert store._required(store.item_key('one', 'a'))['outcome'] == 'accepted'
    assert store._required(store.item_key('one', 'b'))['outcome'] == 'cancelled'
    assert directory.get('session!one') is None
    assert directory.notices() == [dict(sessionId='one', waveId=parent['waveId'], affectedCount=2, closedAt=now[0])]
    directory.reconcile(store, driven.append)
    assert len(driven) == 1 and len(directory.notices()) == 1
    directory.acknowledge('one')
    directory.acknowledge('one')
    assert not directory.notices()
    assert not Sessions(directory.table, 'another-library', clock=lambda:now[0]).notices()


def test_healthy_heartbeats_do_not_write_directory(world):
    store, directory, now = world
    admit(store, directory)
    before = len(directory.table.transactions)
    for _ in range(6):
        now[0] += 90
        assert renew(store, 'one')['leaseActive']
    assert len(directory.table.transactions) == before
    directory.reconcile(store, lambda *a:pytest.fail('Active upload'))
    assert directory.get('session!one')
    assert not directory.notices()


def test_heartbeat_winning_after_stale_read_prevents_closure(world):
    store, directory, now = world
    admit(store, directory)
    now[0] = 1301
    def heartbeat():
        now[0] = 1299
        assert renew(store, 'one')['leaseActive']
        now[0] = 1301
    directory.table.before_commit = heartbeat
    directory.reconcile(store, lambda *a:pytest.fail('Must not close'))
    assert store._required(store.session_key('one'))['state'] == 'open'
    assert not directory.notices()


def test_another_core_respects_claim_then_recovers_after_owner_dies(world):
    store, directory, now = world
    admit(store, directory)
    row = directory.get('session!one')
    directory.table.submit_transaction([directory.change(row, dict(row, claimUntil=now[0]+500, claimToken='dead-core'))])
    now[0] += 301
    assert directory.reconcile(store, lambda *a:pytest.fail('Other owner active'))
    now[0] += 201
    directory.reconcile(store, lambda *a:None)
    assert len(directory.notices()) == 1


@pytest.mark.parametrize('failure', ['handoff', 'notification'])
def test_terminal_interruption_retains_entry_and_replays_handoff_and_notice(world, failure):
    store, directory, now = world
    admit(store, directory)
    now[0] += 301
    def failing_handoff(wave):
        raise AzureError('Stage relay unavailable')
    original = directory.table.submit_transaction
    def failing_notice(operations):
        if any(op[1]['RowKey'].startswith('notice!') for op in operations):
            raise AzureError('Notice storage unavailable')
        return original(operations)
    if failure == 'notification':
        directory.table.submit_transaction = failing_notice
    with pytest.raises(AzureError):
        directory.reconcile(store, failing_handoff if failure == 'handoff' else lambda *a:None)
    assert directory.get('session!one')
    assert not directory.notices()
    assert store._required(store.session_key('one'))['state'] == 'abandoned'
    directory.table.submit_transaction = original
    directory.reconcile(store, lambda *a:None)
    assert len(directory.notices()) == 1
    assert directory.get('session!one') is None


def test_normal_completion_has_no_stale_notice(world):
    store, directory, now = world
    directory.track('one')
    parent=store.open_session('one',0,lease_seconds=300)
    store.close('one',parent['manifestRevision'],'complete')
    directory.reconcile(store,lambda *a:None)
    assert directory.get('session!one') is None
    assert not directory.notices()


def test_finalization_grace_is_resumable_and_never_starts_an_expiry_wake(world):
    store,directory,now=world
    admit(store,directory)
    item=store._required(store.item_key('one','b'))
    store.table.submit_transaction([store._change(item,dict(item,finalizeLeaseUntil=1600))])
    now[0]=1301
    assert directory.reconcile(store,lambda *a:pytest.fail('Finalize not drained'))
    assert store._required(store.session_key('one'))['abandoning']
    assert not directory.notices()
    now[0]+=31
    directory.reconcile(store,lambda *a:None)
    assert len(directory.notices())==1


def test_absent_admission_is_fenced_but_a_racing_new_session_is_preserved(world):
    store,directory,now=world
    directory.track('one')
    now[0]+=301
    def upload_commits():
        store.open_session('one',0,lease_seconds=300)
    directory.table.before_commit=upload_commits
    directory.reconcile(store,lambda *a:pytest.fail('Fresh admission must survive'))
    assert store._required(store.session_key('one'))['state']=='open'
    assert directory.get('session!one')


def test_directory_cursor_does_not_starve_stale_rows_behind_healthy_ones(world):
    store,directory,now=world
    for i in range(25):
        name=f'{i:02}'
        directory.track(name)
        store.open_session(name,0,lease_seconds=300)
    now[0]+=301
    for i in range(20):
        row=store._required(store.session_key(f'{i:02}'))
        store.table.submit_transaction([store._change(row,dict(row,leaseExpiresAt=now[0]+300))])
    assert directory.reconcile(store,lambda *a:None)
    directory.reconcile(store,lambda *a:None)
    assert len(directory.notices())==5


def test_late_migration_replay_does_not_renotify_acknowledged_session(world):
    store,directory,now=world
    admit(store,directory)
    now[0]+=301
    directory.reconcile(store,lambda *a:None)
    directory.acknowledge('one')
    directory.track('one')  # Old survey read OPEN before another Core sealed it.
    directory.reconcile(store,lambda *a:None)
    assert not directory.notices()
    assert directory.get('session!one') is None


def test_prior_failed_files_are_included_in_notice(world):
    store,directory,now=world
    admit(store,directory)
    store.resolve('one','b',outcome='failed')
    now[0]+=301
    directory.reconcile(store,lambda *a:None)
    assert directory.notices()[0]['affectedCount']==2


def test_read_receipts_cannot_hide_later_unread_notices(world):
    store,directory,now=world
    for i in range(105):
        directory.table.submit_transaction([('create',dict(PartitionKey=directory.partition,
            RowKey=f'notice!{i:03}',sessionId=str(i),waveId='wave',affectedCount=1,
            closedAt=now[0],expiresAt=now[0]+100,read=i<104))])
    assert [n['sessionId'] for n in directory.notices()]==['104']


def test_paused_execution_retains_terminal_directory_until_handoff_can_run(world):
    store,directory,now=world
    admit(store,directory)
    now[0]+=301
    assert directory.reconcile(store,lambda *args:False)
    assert directory.get('session!one')
    assert not directory.notices()
    directory.reconcile(store,lambda *args:True)
    assert len(directory.notices())==1


def test_old_session_without_failure_counters_still_reports_failed_items(world):
    store,directory,now=world
    admit(store,directory)
    store.resolve('one','b',outcome='failed')
    old=store._required(store.session_key('one'))
    legacy=dict(old)
    legacy.pop('failedCount',None)
    legacy.pop('cancelledCount',None)
    store.table.submit_transaction([store._change(old,legacy)])
    now[0]+=301
    directory.reconcile(store,lambda *a:None)
    assert directory.notices()[0]['affectedCount']==2
