from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import app
import index_events
from routes import tools, index_events as event_routes


@pytest.mark.parametrize('handler,path', [
    (event_routes.negotiate_index_events, '/api/index-events/negotiate'),
    (tools.negotiate_index_events, '/api/tools/index-events/negotiate'),
])
def test_negotiate_binds_to_authenticated_library_without_send_or_join_permissions(monkeypatch, handler, path):
    client=SimpleNamespace(get_client_access_token=Mock(return_value={'url':'wss://service/client?token=private'}))
    monkeypatch.setenv('INDEX_EVENTS_ENDPOINT','https://service')
    monkeypatch.setattr(index_events,'_client',lambda:client)
    monkeypatch.setattr(app,'_require_user_id',lambda:('library-A',None))
    with app.app.test_request_context(path, method='POST',json={'libraryId':'library-B'}):
        response=handler()
    assert response.headers['Cache-Control']=='no-store'
    assert response.get_json()['libraryId']=='library-A'
    client.get_client_access_token.assert_called_once_with(groups=[index_events.group('library-A')],roles=[],minutes_to_expire=5)


@pytest.mark.parametrize('handler', [event_routes.negotiate_index_events, tools.negotiate_index_events])
def test_negotiation_requires_authentication(monkeypatch, handler):
    monkeypatch.setattr(app,'_require_user_id',lambda:(None,('unauthorized',401)))
    assert handler()==('unauthorized',401)


def test_core_negotiation_handles_service_failure_without_indexer_fallback(monkeypatch):
    monkeypatch.setattr(app, '_require_user_id', lambda: ('library-A', None))
    monkeypatch.setattr(index_events, 'negotiate', Mock(side_effect=OSError('offline')))
    with app.app.test_client() as client:
        response = client.post('/api/index-events/negotiate', json={})
    assert response.status_code == 503
    assert response.get_json() == {'available': False}


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
