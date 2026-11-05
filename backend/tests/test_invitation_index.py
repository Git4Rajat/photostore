import pytest

import library_utils
from fakes import make_store


def invite(store, email='friend@example.com', **kwargs):
    return store.create_invite(library_id='owner', email=email, target_type='join', invited_by='owner', **kwargs)


def test_core_requires_explicit_migration_without_history_scan(monkeypatch):
    store = make_store()
    store.require_invite_index = True
    monkeypatch.setattr(store.invites, 'query_entities', lambda *_a, **_k: pytest.fail('core scanned history'))
    with pytest.raises(library_utils.InviteIndexNotReady):
        store.pending_invites('owner')


def test_index_skips_expired_history_and_preserves_canonical_ids(monkeypatch):
    monkeypatch.setattr(library_utils, '_now', lambda: 10000)
    store = make_store()
    store.ensure_personal_library('owner')
    for number in range(1000):
        store.invites.upsert_entity({'PartitionKey': 'owner', 'RowKey': f'inv-old-{number}',
                                    'status': 'accepted', 'expiresAt': 9999})
    invite(store, 'expired@example.com', ttl_seconds=-1)
    token = invite(store)
    canonical = store.get_invite_by_token(token)
    query = store.invites.query_entities
    queries = []
    def tracked(filter_str, **kwargs):
        queries.append(filter_str)
        return query(filter_str, **kwargs)
    monkeypatch.setattr(store.invites, 'query_entities', tracked)
    store.require_invite_index = True
    assert store.pending_invites('owner') == [canonical]
    assert len(queries) == 1
    assert "RowKey ge 'pending:000000010001:'" in queries[0]
    assert store.find_pending_invite_for_email('owner', 'FRIEND@example.com')['RowKey'] == canonical['RowKey']
    store.revoke_invite('owner', canonical['RowKey'])
    assert store.pending_invites('owner') == []


def test_legacy_backfill_is_retryable_and_retains_token_acceptance(monkeypatch):
    store = make_store()
    token = invite(store)
    canonical = store.get_invite_by_token(token)
    # Simulate the old schema: canonical invitation and token only.
    for key in list(store.invites.rows):
        if key[1].startswith('pending:'):
            del store.invites.rows[key]
    store.require_invite_index = True
    original = store.invites.upsert_entity
    def fail_marker(entity, **kwargs):
        if entity['RowKey'] == '_pending_index_v1':
            raise RuntimeError('interrupted')
        return original(entity, **kwargs)
    monkeypatch.setattr(store.invites, 'upsert_entity', fail_marker)
    with pytest.raises(RuntimeError, match='interrupted'):
        store.backfill_invite_index('owner')
    with pytest.raises(library_utils.InviteIndexNotReady):
        store.pending_invites('owner')
    monkeypatch.setattr(store.invites, 'upsert_entity', original)
    assert store.backfill_invite_index('owner') == 1
    assert store.backfill_invite_index('owner') == 1
    assert store.get_invite_by_token(token) == canonical
    assert store.pending_invites('owner') == [canonical]
    store.mark_invite_accepted(canonical)
    assert store.pending_invites('owner') == []
    assert store.get_invite_by_token(token) is None


def test_backfill_cannot_resurrect_concurrently_revoked_invite(monkeypatch):
    store = make_store()
    token = invite(store)
    canonical = store.get_invite_by_token(token)
    original = store.invites.upsert_entity
    def revoke_during_backfill(entity, **kwargs):
        if entity['RowKey'].startswith('pending:'):
            store.revoke_invite('owner', canonical['RowKey'])
        return original(entity, **kwargs)
    monkeypatch.setattr(store.invites, 'upsert_entity', revoke_during_backfill)
    store.backfill_invite_index('owner')
    assert store.pending_invites('owner') == []


def test_failed_create_transaction_does_not_reserve_a_seat(monkeypatch):
    store = make_store()
    store.ensure_personal_library('owner')
    def fail(_ops):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(store.invites, 'submit_transaction', fail)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        invite(store)
    assert store.pending_invites('owner') == []
    token_rows = [row for (pk, _), row in store.invites.rows.items() if pk == 'token']
    assert len(token_rows) == 1
    assert store._get(store.invites, 'owner', token_rows[0]['inviteId']) is None


def test_index_storage_failure_is_not_an_empty_pending_list(monkeypatch):
    store = make_store()
    store.ensure_personal_library('owner')
    def fail(*_a, **_k):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(store.invites, 'query_entities', fail)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        store.pending_invites('owner')


def test_recent_closed_candidates_do_not_need_canonical_point_reads(monkeypatch):
    store = make_store()
    store.ensure_personal_library('owner')
    for number in range(30):
        token = invite(store, f'closed{number}@example.com')
        canonical = store.get_invite_by_token(token)
        if number % 2:
            store.mark_invite_accepted(canonical)
        else:
            store.revoke_invite('owner', canonical['RowKey'])
    get = store.invites.get_entity
    reads = []
    def tracked(partition_key, row_key):
        reads.append(row_key)
        return get(partition_key, row_key)
    monkeypatch.setattr(store.invites, 'get_entity', tracked)
    assert store.pending_invites('owner') == []
    assert reads == ['_pending_index_v1']


def test_lookup_keys_cannot_be_used_as_revoke_ids():
    store = make_store()
    store.ensure_personal_library('owner')
    token = invite(store)
    canonical = store.get_invite_by_token(token)
    lookup = store._invite_lookup(canonical)
    assert store.revoke_invite('owner', lookup['RowKey']) is False
    assert store.get_invite_by_token(token) == canonical
    assert store.pending_invites('owner') == [canonical]
