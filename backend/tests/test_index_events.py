from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import app
import index_events
from routes import tools


def test_negotiate_binds_to_authenticated_library_without_send_or_join_permissions(monkeypatch):
    client=SimpleNamespace(get_client_access_token=Mock(return_value={'url':'wss://service/client?token=private'}))
    monkeypatch.setenv('INDEX_EVENTS_ENDPOINT','https://service')
    monkeypatch.setattr(index_events,'_client',lambda:client)
    monkeypatch.setattr(app,'_require_user_id',lambda:('library-A',None))
    with app.app.test_request_context('/api/tools/index-events/negotiate', method='POST',json={'libraryId':'library-B'}):
        response=tools.negotiate_index_events()
    assert response.headers['Cache-Control']=='no-store'
    assert response.get_json()['libraryId']=='library-A'
    client.get_client_access_token.assert_called_once_with(groups=[index_events.group('library-A')],roles=[],minutes_to_expire=5)


def test_negotiation_requires_authentication(monkeypatch):
    monkeypatch.setattr(app,'_require_user_id',lambda:(None,('unauthorized',401)))
    assert tools.negotiate_index_events()==('unauthorized',401)


@pytest.mark.parametrize('status,result,expected',[
    ('done',{},True),('failed',{},False),('queued',{},False),
    ('done',{'status':'deferred'},False),('done',{'status':'no_base'},False),
])
def test_only_completed_publications_emit_push(monkeypatch,status,result,expected):
    monkeypatch.setenv('INDEX_EVENTS_ENDPOINT','https://service')
    monkeypatch.setattr(app,'_get_job_row',lambda *a:{'status':status,'result':result})
    send=Mock()
    monkeypatch.setattr(index_events,'publish',send)
    app._notify_index_ready('A','job','search')
    assert bool(send.call_count)==expected


def test_push_failure_cannot_fail_the_index_job(monkeypatch):
    monkeypatch.setenv('INDEX_EVENTS_ENDPOINT','https://service')
    monkeypatch.setattr(app,'_get_job_row',lambda *a:{'status':'done'})
    monkeypatch.setattr(index_events,'publish',Mock(side_effect=OSError('offline')))
    app._notify_index_ready('A','job','people')


def test_push_disabled_has_no_external_dependency(monkeypatch):
    monkeypatch.delenv('INDEX_EVENTS_ENDPOINT',raising=False)
    assert index_events.negotiate('A')=={'available':False}
