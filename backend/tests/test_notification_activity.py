"""What the bell learns from the server: shared albums someone opened, invites about to end,
and sending an invite again."""
import time
from types import SimpleNamespace

import pytest

import app
import library_utils
import routes.public as public
from fakes import make_store


class AlbumsTable:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.updates = []
        self.queries = []

    def update_entity(self, entity, mode=None):
        self.updates.append((dict(entity), mode))

    def query_entities(self, query, select=None):
        self.queries.append((query, select))
        return iter(self.rows)


class TestAlbumOpens:
    @pytest.fixture
    def opens(self, monkeypatch):
        state = {'entity': {'PartitionKey': 'owner', 'RowKey': 'album1', 'name': 'Trip', 'isPublic': True}}
        table = AlbumsTable()
        public._recent_opens.clear()
        monkeypatch.setattr(app, 'albums_table_client', table)
        monkeypatch.setattr(app, '_find_public_album_by_token', lambda token: state['entity'] if token == 'tok' else None)
        monkeypatch.setattr(app, '_album_is_expired', lambda e: False)
        monkeypatch.setattr(app, 'album_store', SimpleNamespace(page=lambda e, offset, limit: ([], 0)))
        monkeypatch.setattr(app, '_get_metadata_entities', lambda owner, names: {})
        return state, table, app.app.test_client()

    def test_the_first_page_counts_one_visit_per_window(self, opens):
        state, table, client = opens
        assert client.get('/api/public/albums/tok').status_code == 200
        assert client.get('/api/public/albums/tok').status_code == 200          # a reload
        assert client.post('/api/public/albums/tok', json={'offset': 120}).status_code == 200  # paging
        assert len(table.updates) == 1
        written, mode = table.updates[0]
        assert written['PartitionKey'] == 'owner' and written['RowKey'] == 'album1'
        assert written['openCount'] == 1 and written['lastOpenedAt'] > 0
        assert mode == app.UpdateMode.MERGE

    def test_another_replica_already_counted_the_visit(self, opens):
        state, table, client = opens
        state['entity']['lastOpenedAt'] = time.time() - 60
        client.get('/api/public/albums/tok')
        assert table.updates == []
        state['entity']['lastOpenedAt'] = time.time() - public.OPEN_WINDOW_SECONDS - 1
        state['entity']['openCount'] = 4
        public._recent_opens.clear()
        client.get('/api/public/albums/tok')
        assert table.updates[0][0]['openCount'] == 5

    def test_the_snapshot_beacon_counts_public_albums_only(self, opens):
        state, table, client = opens
        assert client.post('/api/public/albums/nope/opened').status_code == 204
        state['entity']['accessCode'] = '1234'
        assert client.post('/api/public/albums/tok/opened').status_code == 204
        assert table.updates == []
        del state['entity']['accessCode']
        assert client.post('/api/public/albums/tok/opened').status_code == 204
        assert len(table.updates) == 1

    def test_a_failed_write_never_breaks_the_album(self, opens, monkeypatch):
        state, table, client = opens

        def broken(*a, **k):
            raise RuntimeError('storage down')
        monkeypatch.setattr(table, 'update_entity', broken)
        assert client.get('/api/public/albums/tok').status_code == 200


class TestActivity:
    @pytest.fixture
    def activity(self, monkeypatch):
        now = time.time()
        table = AlbumsTable([
            {'RowKey': 'a1', 'name': 'Malmö nights', 'lastOpenedAt': now - 600, 'openCount': 3},
            {'RowKey': 'gone', 'name': 'Deleted', 'lastOpenedAt': now - 60, 'deleted': True},
        ])
        invites = [
            {'RowKey': 'inv-soon', 'emailNorm': 'dev@example.com', 'expiresAt': int(now + 3600)},
            {'RowKey': 'inv-later', 'emailNorm': 'later@example.com', 'expiresAt': int(now + 3 * 86400)},
        ]
        owner = {'is': True}
        monkeypatch.setattr(app, 'albums_table_client', table)
        monkeypatch.setattr(app, '_albums_table_available', lambda: True)
        monkeypatch.setattr(app, '_require_library_context', lambda **kw: ('acct', 'lib', None))
        monkeypatch.setattr(app, 'library_store', SimpleNamespace(
            is_owner=lambda account, library: owner['is'] and (account, library) == ('acct', 'lib'),
            pending_invites=lambda library: invites))
        monkeypatch.setattr(app.email_utils, 'masked_recipient', lambda email: 'd**@example.com')
        return table, owner, app.app.test_client(), now

    def test_lists_recent_opens_and_invites_in_their_last_day(self, activity):
        table, owner, client, now = activity
        body = client.get('/api/activity').json
        assert body['albumOpens'] == [{'albumId': 'a1', 'name': 'Malmö nights', 'openedAt': now - 600, 'opens': 3}]
        assert body['invitesEnding'] == [{'inviteId': 'inv-soon', 'email': 'd**@example.com', 'expiresAt': int(now + 3600)}]
        query, select = table.queries[0]
        assert query.startswith("PartitionKey eq 'lib' and lastOpenedAt gt ")
        assert 'lastOpenedAt' in select

    def test_only_the_owner_hears_about_invites(self, activity):
        table, owner, client, now = activity
        owner['is'] = False
        assert client.get('/api/activity').json['invitesEnding'] == []

    def test_one_failing_half_leaves_the_other(self, activity, monkeypatch):
        table, owner, client, now = activity

        def broken(*a, **k):
            raise RuntimeError('table down')
        monkeypatch.setattr(table, 'query_entities', broken)
        body = client.get('/api/activity').json
        assert body['albumOpens'] == [] and len(body['invitesEnding']) == 1


class TestResend:
    @pytest.fixture
    def resend(self, monkeypatch):
        store = make_store()
        store.require_invite_index = True
        store.create_user(email='owner@example.com', user_id='owner', password_hash='unused')
        store.ensure_personal_library('owner')
        sent = []
        monkeypatch.setattr(app, 'library_store', store)
        monkeypatch.setattr(app, 'SESSION_SECRET', 'resend-test-secret-with-at-least-32-bytes-long')
        monkeypatch.setattr(app, 'AUTH_MODE', 'password')
        monkeypatch.setattr(app.email_utils, 'is_configured', lambda: True)
        monkeypatch.setattr(app.email_utils, 'send_invite_email', lambda email, url, **k: sent.append((email, url)))
        headers = {'Authorization': f"Bearer {app._issue_session_for('owner', library_id='owner')}"}
        client = app.app.test_client()
        assert client.post('/api/library/invite', headers=headers, json={'email': 'dev@example.com'}).status_code == 200
        old = store.pending_invites('owner')[0]
        # A resend comes a day or two later, well past the 30 s gap between invite emails.
        store.invites.rows.pop((library_utils._THROTTLE_PK, 'owner'))
        return store, client, headers, sent, old

    def test_sends_a_fresh_link_and_retires_the_old_one(self, resend):
        store, client, headers, sent, old = resend
        response = client.post('/api/library/invite/resend', headers=headers, json={'inviteId': old['RowKey']})
        assert response.status_code == 200
        pending = store.pending_invites('owner')
        assert [inv['emailNorm'] for inv in pending] == ['dev@example.com']
        assert pending[0]['RowKey'] != old['RowKey']
        assert len(sent) == 2 and sent[1][0] == 'dev@example.com' and sent[1][1] != sent[0][1]
        token = sent[1][1].split('token=')[1]
        assert store.get_invite_by_token(token)['RowKey'] == pending[0]['RowKey']

    def test_a_failed_email_keeps_the_old_invite(self, resend, monkeypatch):
        store, client, headers, sent, old = resend

        def fail(*a, **k):
            raise RuntimeError('smtp down')
        monkeypatch.setattr(app.email_utils, 'send_invite_email', fail)
        response = client.post('/api/library/invite/resend', headers=headers, json={'inviteId': old['RowKey']})
        assert response.status_code == 502
        assert [inv['RowKey'] for inv in store.pending_invites('owner')] == [old['RowKey']]

    def test_an_invite_that_is_gone_is_404(self, resend):
        store, client, headers, sent, old = resend
        store.revoke_invite('owner', old['RowKey'])
        response = client.post('/api/library/invite/resend', headers=headers, json={'inviteId': old['RowKey']})
        assert response.status_code == 404
