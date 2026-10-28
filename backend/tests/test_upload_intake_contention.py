"""Cancellation/admission interleavings behind refresh -> Discard -> upload."""
import pytest
from upload_waves import ShadowWaves, Conflict
from wave_settlement import abandon
from test_upload_waves import AtomicTable


def setup(count=25):
    table=AtomicTable(); store=ShadowWaves(table,'library',clock=lambda:1000)
    opened=store.open_session('old',count)
    for start in range(0,count,20):
        store.register('old',[dict(itemId=f'{i:03}',filename=f'{i}.jpg') for i in range(start,min(start+20,count))])
    return store,table,opened['waveId']


def test_abandon_page_is_atomic_and_preserves_accepted_members():
    store,table,wave=setup()
    store.resolve('old','000',outcome='accepted',photo_id='photo',processing_revision='rev',filename='0.jpg',required_steps=['face'])
    before=dict(store._required(store.item_key('old','000')))
    initial=len(table.transactions)
    parent=store._required(store.session_key('old'))
    result=abandon(store,'old',parent['manifestRevision'],reason='User discarded after refresh')
    assert len(table.transactions)==initial+1
    assert result['resolvedCount']==20 and result['abandonmentNeedsMore']
    assert dict(store._required(store.item_key('old','000')))==before
    result=abandon(store,'old',result['manifestRevision'],reason='User discarded after refresh')
    assert result['state']=='abandoned' and result['resolvedCount']==25
    assert store._required(store.wave_key(wave))['memberCount']==1


def test_live_finalize_lease_refuses_entire_page_without_partial_cancellation():
    store,table,_=setup()
    old=store._required(store.item_key('old','019'))
    table.submit_transaction([store._change(old,dict(old,finalizeLeaseUntil=1001))])
    initial=len(table.transactions);parent=store._required(store.session_key('old'))
    with pytest.raises(Conflict,match='still in flight'):
        abandon(store,'old',parent['manifestRevision'],reason='discard')
    assert len(table.transactions)==initial
    assert store._required(store.session_key('old'))['resolvedCount']==0


def test_finalize_winning_the_race_is_preserved_on_explicit_revision_refresh():
    store,table,wave=setup()
    parent=store._required(store.session_key('old'))
    table.before_commit=lambda:store.resolve('old','000',outcome='accepted',photo_id='p',processing_revision='r',filename='0.jpg',required_steps=['face'])
    with pytest.raises(Conflict,match='revision is stale'):
        abandon(store,'old',parent['manifestRevision'],reason='discard')
    assert store._required(store.session_key('old'))['resolvedCount']==1
    fresh=store._required(store.session_key('old'))
    abandon(store,'old',fresh['manifestRevision'],reason='discard')
    assert store._required(store.item_key('old','000'))['outcome']=='accepted'
    assert store._required(store.wave_key(wave))['memberCount']==1


def test_registration_retries_against_one_atomic_discard_page():
    store,table,_=setup()
    store.open_session('new',20)
    parent=store._required(store.session_key('old'))
    table.before_commit=lambda:abandon(store,'old',parent['manifestRevision'],reason='discard')
    result=store.register('new',[dict(itemId=f'n{i}',filename=f'n{i}.jpg') for i in range(20)])
    assert result['registeredCount']==20
    assert store._required(store.session_key('old'))['resolvedCount']==20
