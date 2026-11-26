"""Publishing a face lookup row conditions its write on the ETag of the writer's own last write or read.

The row's begin, per-mutation renewal and publish used to read it three or more times; the publish read is now
the writer's own ETag. A change by anyone else still fails the publish, as reading the changed row did.
"""
import pytest

import storage_utils
from tests.test_face_by_filename_lookup import lookup_ctx, _face  # noqa: F401

SWITCH = 'IPWORK_FACE_LOOKUP_REUSE'


@pytest.fixture(autouse=True)
def clean_memory():
    storage_utils._OWN_LOOKUP_ROWS.__dict__.clear()
    yield
    storage_utils._OWN_LOOKUP_ROWS.__dict__.clear()


def count_reads(lookup):
    counts = {'reads': 0}
    original = lookup.get_entity

    def get(*args, **kwargs):
        counts['reads'] += 1
        return original(*args, **kwargs)
    lookup.get_entity = get
    return counts


def store(filename, faces):
    return storage_utils._store_client_face_entities('u1', filename, faces)


@pytest.mark.parametrize('faces', [1, 3])
def test_publishing_saves_one_lookup_read_and_leaves_the_same_row(lookup_ctx, monkeypatch, faces):
    """Offline benchmark: lookup reads for one photo with faces, switch off vs on; rows must be identical."""
    seen = {}
    for enabled in ('false', 'true'):
        monkeypatch.setenv(SWITCH, enabled)
        _, lookup = lookup_ctx
        lookup.rows.clear(); lookup.versions.clear()
        counts = count_reads(lookup)
        ids = store('p.jpg', [_face(i * 20) for i in range(faces)])
        row = {k: v for k, v in dict(lookup.rows[('u1', 'p.jpg')]).items() if k != 'generation'}
        seen[enabled] = (counts['reads'], len(ids), row)
        lookup.get_entity = type(lookup).get_entity.__get__(lookup)
    print(f'\nfaces={faces}: lookup reads off={seen["false"][0]} on={seen["true"][0]}')
    assert seen['true'][0] == seen['false'][0] - 1
    assert seen['true'][1:] == seen['false'][1:]
    assert seen['true'][2]['state'] == 'complete'


def test_row_taken_over_before_publish_still_fails_the_publish(lookup_ctx):
    _, lookup = lookup_ctx
    generation, _ = storage_utils._begin_face_filename_write('u1', 'p.jpg')
    stolen = dict(lookup.rows[('u1', 'p.jpg')], generation='b' * 32)
    lookup.update_entity(stolen, mode=storage_utils.UpdateMode.REPLACE, etag=str(lookup.versions[('u1', 'p.jpg')]),
                         match_condition=storage_utils.MatchConditions.IfNotModified)
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError):
        storage_utils._finish_face_filename_write('u1', 'p.jpg', generation, ['f1'])
    assert lookup.rows[('u1', 'p.jpg')]['generation'] == 'b' * 32  # The other writer's row is untouched.
    assert lookup.rows[('u1', 'p.jpg')]['state'] == 'writing'


def test_a_renewal_refreshes_the_remembered_etag(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    generation, _ = storage_utils._begin_face_filename_write('u1', 'p.jpg')
    monkeypatch.setattr(storage_utils, '_FACE_FILENAME_LEASE_SECONDS', 0.0001)  # Every renewal now rewrites the row.
    storage_utils._renew_face_filename_write('u1', 'p.jpg', generation)
    counts = count_reads(lookup)
    storage_utils._finish_face_filename_write('u1', 'p.jpg', generation, ['f1'])
    assert counts['reads'] == 0
    assert lookup.rows[('u1', 'p.jpg')]['state'] == 'complete'


def test_a_release_always_reads_the_row(lookup_ctx):
    _, lookup = lookup_ctx
    generation, _ = storage_utils._begin_face_filename_write('u1', 'p.jpg')
    counts = count_reads(lookup)
    storage_utils._finish_face_filename_write('u1', 'p.jpg', generation, None)
    assert counts['reads'] == 1
    assert lookup.rows[('u1', 'p.jpg')]['state'] == 'dirty'


def test_an_expired_lease_is_not_published_from_memory(lookup_ctx, monkeypatch):
    _, lookup = lookup_ctx
    monkeypatch.setattr(storage_utils, '_FACE_FILENAME_LEASE_SECONDS', -5)
    generation, _ = storage_utils._begin_face_filename_write('u1', 'p.jpg')
    with pytest.raises(storage_utils.FaceFilenameLookupRetryableError, match='lease expired'):
        storage_utils._finish_face_filename_write('u1', 'p.jpg', generation, ['f1'])


def test_a_store_that_returns_no_etag_falls_back_to_reading(lookup_ctx):
    _, lookup = lookup_ctx
    update, create = lookup.update_entity, lookup.create_entity
    lookup.create_entity = lambda entity: create(entity) and None
    lookup.update_entity = lambda *a, **k: update(*a, **k) and None
    generation, _ = storage_utils._begin_face_filename_write('u1', 'p.jpg')
    counts = count_reads(lookup)
    storage_utils._finish_face_filename_write('u1', 'p.jpg', generation, ['f1'])
    assert counts['reads'] == 1


def test_crash_between_begin_and_publish_leaves_a_recoverable_row(lookup_ctx, monkeypatch):
    """A retry after a crash begins a new generation over the expired writer's row; nothing is read from memory."""
    _, lookup = lookup_ctx
    first, _ = storage_utils._begin_face_filename_write('u1', 'p.jpg')
    assert storage_utils._own_lookup_row('u1', 'p.jpg', 'someone-else') is None
    storage_utils._OWN_LOOKUP_ROWS.rows.clear()  # The crashed process's memory is gone.
    row = dict(lookup.rows[('u1', 'p.jpg')], leaseExpiresAt='2000-01-01T00:00:00+00:00')
    lookup.update_entity(row, mode=storage_utils.UpdateMode.REPLACE, etag=str(lookup.versions[('u1', 'p.jpg')]),
                         match_condition=storage_utils.MatchConditions.IfNotModified)
    assert len(store('p.jpg', [_face(0)])) == 1
    assert lookup.rows[('u1', 'p.jpg')]['state'] == 'complete'
    assert lookup.rows[('u1', 'p.jpg')]['generation'] != first


def test_remembered_rows_are_bounded(lookup_ctx):
    for i in range(200):
        storage_utils._remember_own_lookup_row('u1', f'{i}.jpg', 'g', {'a': 1}, {'etag': '1'})
    assert len(storage_utils._OWN_LOOKUP_ROWS.rows) <= 65
