"""The dedicated Clean library (library_clean.py, docs/library-clean-dedicated.md).

Pins what the measured legacy clean got wrong -- face embeddings, anonymized blobs of filenames another
library also holds, public album snapshots, export zips, preview cache and the hash-keyed partitions were
left behind -- and the properties the dedicated engine is for: bounded transactions and blob batches, a
fence check per round of writes, retries, and a verification that fails the job instead of reporting done
with data left.
"""
import re
from collections import defaultdict
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

import app
import library_clean
from fakes import FakeTable

LIB, OTHER = 'lib', 'other'


class Table(FakeTable):
    """FakeTable plus the shapes the clean uses: bare column filters and service-like transaction limits."""

    def query_entities(self, filter_str, select=None, **kwargs):
        match = re.fullmatch(r"(userId|RowKey) eq '((?:[^']|'')*)'", filter_str.strip())
        if match:
            field, value = match.group(1), match.group(2).replace("''", "'")
            return self._project([dict(row) for row in self.rows.values() if str(row.get(field, '')) == value],
                                 select)
        return super().query_entities(filter_str, select=select, **kwargs)

    def submit_transaction(self, operations):
        operations = list(operations)
        assert 1 <= len(operations) <= 100
        assert len({op[1]['PartitionKey'] for op in operations}) == 1
        return super().submit_transaction(operations)


class Container:
    def __init__(self, service, name):
        self.service, self.name = service, name

    @property
    def blobs(self):
        return self.service.blobs[self.name]

    def delete_blobs(self, *names, delete_snapshots=None, raise_on_any_failure=True):
        assert 1 <= len(names) <= 256
        self.service.batches.append((self.name, len(names)))
        responses = []
        for name in names:
            if name in self.service.refuse:
                responses.append(SimpleNamespace(status_code=500))
            elif self.blobs.pop(name, None) is None:
                responses.append(SimpleNamespace(status_code=404))
            else:
                responses.append(SimpleNamespace(status_code=202))
        return iter(responses)

    def delete_blob(self, name, delete_snapshots=None):
        if name in self.service.refuse:
            raise HttpResponseError('refused')
        if self.blobs.pop(name, None) is None:
            raise ResourceNotFoundError('BlobNotFound')

    def list_blobs(self, name_starts_with=''):
        return [SimpleNamespace(name=name) for name in sorted(self.blobs) if name.startswith(name_starts_with)]

    def get_blob_client(self, name):
        return SimpleNamespace(exists=lambda: name in self.blobs)


class Blobs:
    def __init__(self):
        self.blobs = defaultdict(dict)
        self.batches = []
        self.refuse = set()

    def get_container_client(self, name):
        return Container(self, name)

    def put(self, container, *names):
        for name in names:
            self.blobs[container][name] = b'x'


CONTAINERS = {'images': 'images', 'thumbnails': 'thumbnails', 'covers': 'covers', 'exports': 'exports',
              'merge_payloads': 'merge-payloads', 'people_embedding_index': 'people-embedding-index', 'web': '$web'}


def delete_snapshot(container, token):
    names = [b.name for b in container.list_blobs(name_starts_with=f's/{token}/')]
    for name in names:
        container.delete_blob(name)
    return len(names)


def world():
    tables = {key: Table() for key in set(library_clean.TABLE_CONSTANTS.values())}
    blobs = Blobs()
    return tables, blobs


def stores(tables, blobs, **kwargs):
    return library_clean.Stores(tables=tables, blobs=blobs, containers=CONTAINERS,
                                delete_snapshot=delete_snapshot, **kwargs)


def seed(tables, blobs, lib, *, anon_prefix):
    """One of everything a library can own."""
    t = tables
    for i, name in enumerate(['IMG_0001.HEIC', 'IMG_0002.HEIC']):
        anon = f'{anon_prefix}{i:030d}'
        t['metadata'].upsert_entity({'PartitionKey': lib, 'RowKey': name, 'anonymousImageId': anon})
        t['image_names'].upsert_entity({'PartitionKey': lib, 'RowKey': anon, 'original_filename': name})
        t['filename_owners'].upsert_entity({'PartitionKey': name, 'RowKey': lib})
        blobs.put('images', anon)
        blobs.put('thumbnails', anon, f'metadata/{anon}.bin', f'grid/{anon}.webp', f'preview/{anon}.jpg')
    for key in ('embeddings', 'face_by_filename', 'hashes', 'trash', 'workbench', 'albums_dirty', 'people_dirty'):
        t[key].upsert_entity({'PartitionKey': lib, 'RowKey': 'IMG_0001.HEIC'})
    t['faces'].upsert_entity({'PartitionKey': lib, 'RowKey': f'face-{lib}', 'filename': 'IMG_0001.HEIC'})
    t['face_embeddings'].upsert_entity({'PartitionKey': lib, 'RowKey': f'face-{lib}', 'embedding': 'e'})
    t['people'].upsert_entity({'PartitionKey': lib, 'RowKey': f'person-{lib}'})
    t['person_members'].upsert_entity({'PartitionKey': f'person-{lib}', 'RowKey': f'face-{lib}', 'userId': lib})
    t['person_members'].upsert_entity({'PartitionKey': f'gone-{lib}', 'RowKey': 'face-x', 'userId': lib})
    t['albums'].upsert_entity({'PartitionKey': lib, 'RowKey': f'album-{lib}', 'publicToken': f'tok-{lib}',
                               'shareSnapshotToken': f'tok-{lib}'})
    t['album_members'].upsert_entity({'PartitionKey': f'album-{lib}', 'RowKey': 'IMG_0001.HEIC'})
    t['album_tokens'].upsert_entity({'PartitionKey': f'tok-{lib}', 'RowKey': 'owner', 'userId': lib})
    blobs.put('$web', f's/tok-{lib}/index.html', f's/tok-{lib}/manifest.json', f's/tok-{lib}/t/1.jpg')
    t['merges'].upsert_entity({'PartitionKey': lib, 'RowKey': 'm1', 'payloadBlobName': f'{lib}/m1.json'})
    blobs.put('merge-payloads', f'{lib}/m1.json')
    t['jobs'].upsert_entity({'PartitionKey': lib, 'RowKey': 'ipwork:1', 'jobType': 'ipwork'})
    t['jobs'].upsert_entity({'PartitionKey': lib, 'RowKey': 'libclean:1', 'jobType': 'library_clean'})
    t['jobs'].upsert_entity({'PartitionKey': 'processing-artifacts-' + library_clean.wave_digest(lib),
                             'RowKey': 'manifest!1'})
    t['people_dirty'].upsert_entity({'PartitionKey': 'labels:' + library_clean.sha256(lib), 'RowKey': 'p'})
    sessions = 'library-' + library_clean.wave_digest(lib)
    t['upload_sessions'].upsert_entity({'PartitionKey': sessions, 'RowKey': 'session!1', 'state': 'OPEN'})
    t['upload_sessions'].upsert_entity({'PartitionKey': sessions, 'RowKey': 'migration', 'complete': True})
    t['metadata'].upsert_entity({'PartitionKey': 'clustering_maintenance', 'RowKey': lib})
    scope = library_clean.sha256(lib)
    blobs.put('thumbnails', f'derived/v1/{scope}/k/thumbnail/h.jpg', f'processing-staging/v1/{scope}/k/preview.jpg')
    blobs.put('covers', f'{scope[:16]}/cover.jpg')
    blobs.put('exports', f'{lib}/library-export-part-0.zip')
    blobs.put('people-embedding-index', f'faiss-source-revisions/{scope}')
    t['libraries'].upsert_entity({'PartitionKey': 'library', 'RowKey': lib})
    t['memberships'].upsert_entity({'PartitionKey': f'lib:{lib}', 'RowKey': 'owner'})


def snapshot(tables, blobs):
    return ({key: set(table.rows) for key, table in tables.items()},
            {container: set(names) for container, names in blobs.blobs.items()})


def test_clean_removes_every_location_and_leaves_the_other_library_alone():
    tables, blobs = world()
    seed(tables, blobs, OTHER, anon_prefix='b')
    before = snapshot(tables, blobs)
    seed(tables, blobs, LIB, anon_prefix='a')

    summary = library_clean.run(LIB, stores(tables, blobs), workers=4)

    rows, names = snapshot(tables, blobs)
    kept = {('libraries', ('library', LIB)), ('memberships', (f'lib:{LIB}', 'owner')),
            ('jobs', (LIB, 'libclean:1')),
            ('upload_sessions', ('library-' + library_clean.wave_digest(LIB), 'migration'))}
    left = {(key, row) for key, keys in rows.items() for row in keys if row not in before[0][key]}
    assert left == kept
    assert names == before[1]
    assert summary['photosDeleted'] == 2 and summary['passes'] == 1
    # The other library shares both filenames; its ownership rows and blobs survive.
    assert ('IMG_0001.HEIC', OTHER) in tables['filename_owners'].rows


def test_legacy_filename_blob_goes_only_when_no_other_library_owns_the_name():
    tables, blobs = world()
    for lib in (LIB, OTHER):
        tables['metadata'].upsert_entity({'PartitionKey': lib, 'RowKey': 'shared.jpg'})
        tables['filename_owners'].upsert_entity({'PartitionKey': 'shared.jpg', 'RowKey': lib})
    tables['metadata'].upsert_entity({'PartitionKey': LIB, 'RowKey': 'solo.jpg'})
    tables['filename_owners'].upsert_entity({'PartitionKey': 'solo.jpg', 'RowKey': LIB})
    blobs.put('images', 'shared.jpg', 'solo.jpg')

    library_clean.run(LIB, stores(tables, blobs), workers=4)

    assert set(blobs.blobs['images']) == {'shared.jpg'}
    assert set(tables['filename_owners'].rows) == {('shared.jpg', OTHER)}
    assert set(tables['metadata'].rows) == {(OTHER, 'shared.jpg')}


def test_rerun_on_an_emptied_library_unpublishes_snapshots_an_earlier_clean_left():
    tables, blobs = world()
    # What the legacy clean left: the album row is gone, its token row and public snapshot are not.
    tables['album_tokens'].upsert_entity({'PartitionKey': 'old-token', 'RowKey': 'owner', 'userId': LIB})
    blobs.put('$web', 's/old-token/index.html', 's/old-token/t/1.jpg')
    tables['face_embeddings'].upsert_entity({'PartitionKey': LIB, 'RowKey': 'face-1', 'embedding': 'e'})

    library_clean.run(LIB, stores(tables, blobs), workers=4)

    assert tables['album_tokens'].rows == {} and blobs.blobs['$web'] == {}
    assert tables['face_embeddings'].rows == {}


def test_token_row_naming_another_library_is_not_deleted():
    tables, blobs = world()
    tables['albums'].upsert_entity({'PartitionKey': LIB, 'RowKey': 'a1', 'publicToken': 'reused'})
    tables['album_tokens'].upsert_entity({'PartitionKey': 'reused', 'RowKey': 'owner', 'userId': OTHER})

    library_clean.run(LIB, stores(tables, blobs), workers=4)

    assert ('reused', 'owner') in tables['album_tokens'].rows


def test_writes_are_bounded_batches_and_one_partition_per_transaction():
    tables, blobs = world()
    for i in range(1050):
        anon = f'{i:032d}'
        tables['metadata'].upsert_entity({'PartitionKey': LIB, 'RowKey': f'p{i}', 'anonymousImageId': anon})
        tables['embeddings'].upsert_entity({'PartitionKey': LIB, 'RowKey': f'p{i}'})
        blobs.put('images', anon)

    summary = library_clean.run(LIB, stores(tables, blobs), workers=8)

    assert tables['metadata'].rows == {} and tables['embeddings'].rows == {} and blobs.blobs['images'] == {}
    sizes = sorted(len(ops) for ops in tables['embeddings'].submit_transaction_calls)
    assert sizes == [50] + [100] * 10
    assert max(size for _, size in blobs.batches) == 256
    assert summary['photosDeleted'] == 1050


def test_failed_transaction_falls_back_to_single_deletes():
    tables, blobs = world()

    class Refusing(Table):
        def submit_transaction(self, operations):
            raise HttpResponseError('transaction refused')
    tables['faces'] = Refusing()
    for i in range(5):
        tables['faces'].upsert_entity({'PartitionKey': LIB, 'RowKey': f'f{i}'})

    library_clean.run(LIB, stores(tables, blobs), workers=4)

    assert tables['faces'].rows == {}


def test_transient_errors_are_retried(monkeypatch):
    monkeypatch.setattr(library_clean.time, 'sleep', lambda seconds: None)
    tables, blobs = world()
    tables['filename_owners'].upsert_entity({'PartitionKey': 'a.jpg', 'RowKey': LIB})
    tables['metadata'].upsert_entity({'PartitionKey': LIB, 'RowKey': 'a.jpg', 'anonymousImageId': 'x' * 32})
    real, failures = tables['filename_owners'].delete_entity, []

    def flaky(pk, rk, **kwargs):
        if not failures:
            failures.append(1)
            error = HttpResponseError('throttled')
            error.status_code = 503
            raise error
        return real(pk, rk, **kwargs)
    tables['filename_owners'].delete_entity = flaky

    library_clean.run(LIB, stores(tables, blobs), workers=4)

    assert failures == [1] and tables['filename_owners'].rows == {}


def test_fence_is_checked_per_round_and_a_failed_check_stops_the_writes():
    tables, blobs = world()
    for i in range(40):
        tables['metadata'].upsert_entity({'PartitionKey': LIB, 'RowKey': f'p{i}', 'anonymousImageId': f'{i:032d}'})
        tables['filename_owners'].upsert_entity({'PartitionKey': f'p{i}', 'RowKey': LIB})
    checks = []

    def check():
        checks.append(len(tables['filename_owners'].rows))
        if len(checks) == 3:
            raise RuntimeError('Operator quiescence proof changed')

    with pytest.raises(RuntimeError, match='proof changed'):
        library_clean.run(LIB, stores(tables, blobs, check=check), workers=4)

    # Round size is the worker count: no more than one round of owner deletes ran after the last good check.
    assert len(tables['filename_owners'].rows) >= checks[-1] - 4
    assert len(tables['metadata'].rows) == 40  # metadata goes last, so a retry finds every photo again


def test_data_left_after_a_second_pass_fails_the_job():
    tables, blobs = world()

    class Sticky(Table):
        def delete_entity(self, partition_key, row_key, **kwargs):
            if row_key != 'stuck':
                super().delete_entity(partition_key, row_key, **kwargs)

        def submit_transaction(self, operations):
            raise HttpResponseError('no batch')
    tables['hashes'] = Sticky()
    tables['hashes'].upsert_entity({'PartitionKey': LIB, 'RowKey': 'stuck'})
    tables['hashes'].upsert_entity({'PartitionKey': LIB, 'RowKey': 'fine'})

    with pytest.raises(library_clean.Incomplete) as raised:
        library_clean.run(LIB, stores(tables, blobs), workers=4)

    assert raised.value.residue == {'hashes': 1}
    assert set(tables['hashes'].rows) == {(LIB, 'stuck')}


def test_a_blob_the_service_keeps_refusing_is_reported_not_forgotten():
    tables, blobs = world()
    anon = 'f' * 32
    tables['metadata'].upsert_entity({'PartitionKey': LIB, 'RowKey': 'a.jpg', 'anonymousImageId': anon})
    blobs.put('images', anon)
    blobs.put('thumbnails', anon)
    blobs.refuse.add(anon)

    with pytest.raises(library_clean.Incomplete) as raised:
        library_clean.run(LIB, stores(tables, blobs), workers=4)

    assert raised.value.residue == {'images+thumbnails:per-photo blobs': 2}


def test_progress_is_reported_with_phase_and_counts():
    tables, blobs = world()
    seed(tables, blobs, LIB, anon_prefix='a')
    states = []

    library_clean.run(LIB, stores(tables, blobs, progress=states.append), workers=4)

    assert [s['phase'] for s in states][-1] == 'done'
    assert {'phase1', 'phase2', 'phase3', 'phase4', 'phase5'} <= {s['phase'] for s in states}
    assert states[-1]['photos'] == 2 and states[-1]['rowsDeleted'] > 0


# --- registry completeness ---------------------------------------------------------------------------

def test_every_app_table_and_container_has_a_clean_decision():
    tables = {name for name, value in vars(app).items() if re.fullmatch(r'[A-Z_]+_TABLE', name) and isinstance(value, str)}
    containers = {name for name in vars(app) if re.fullmatch(r'BLOB_[A-Z_]+_CONTAINER', name)}
    assert tables - set(library_clean.TABLE_CONSTANTS) == set()
    assert containers - set(library_clean.CONTAINER_CONSTANTS) == set()
    decided = stores_of(library_clean.REGISTRY)
    for key in list(library_clean.TABLE_CONSTANTS.values()) + list(library_clean.CONTAINER_CONSTANTS.values()):
        assert key in decided, f'{key} has no REGISTRY entry (wipe it, or Keep it with a reason)'


def stores_of(entries):
    """Store keys an entry covers: 'images+thumbnails' covers both, 'jobs:upload-wave-shadow' is jobs."""
    return {part for entry in entries for part in entry.store.split(':')[0].split('+')}


def test_the_app_hands_the_clean_every_store_it_wipes():
    active = stores_of(entry for entry in library_clean.REGISTRY if entry.phase is not None)
    handed = app._library_clean_stores(LIB, None)
    assert active & set(library_clean.TABLE_CONSTANTS.values()) - set(handed.tables) == set()
    assert active & set(library_clean.CONTAINER_CONSTANTS.values()) - set(handed.containers) == set()


# --- app wiring ----------------------------------------------------------------------------------------

@pytest.fixture
def unfenced(monkeypatch):
    """No durable index updates and no blob lease service: the clean's decorators pass straight through."""
    import storage_utils as su
    monkeypatch.setattr(su, 'DURABLE_INDEX_UPDATES', False)
    monkeypatch.setattr(su, '_CTX', {})


def test_the_dedicated_engine_begins_runs_and_finishes_the_wave_fence(monkeypatch, unfenced):
    import processing_cleanup
    events, calls = [], []
    monkeypatch.setattr(processing_cleanup, 'begin_library_cleanup', lambda *a: events.append('begin'))
    monkeypatch.setattr(processing_cleanup, 'finish_library_cleanup', lambda *a, **kw: events.append('finish'))
    monkeypatch.setattr(app, '_finish_library_clean_indexes', lambda library_id: events.append('indexes'))
    monkeypatch.setattr(library_clean, 'run',
                        lambda library_id, stores: (calls.append(stores), events.append('run'))
                        and {'photosDeleted': 0})

    assert app._dedicated_library_clean(LIB) == {'photosDeleted': 0}

    assert events == ['begin', 'run', 'finish']
    calls[0].finish_indexes()
    assert events[-1] == 'indexes'
    assert calls[0].containers['web'] == '$web'


def test_a_failed_clean_does_not_finish_the_wave_fence(monkeypatch, unfenced):
    import processing_cleanup
    events = []
    monkeypatch.setattr(processing_cleanup, 'begin_library_cleanup', lambda *a: events.append('begin'))
    monkeypatch.setattr(processing_cleanup, 'finish_library_cleanup', lambda *a, **kw: events.append('finish'))

    def incomplete(library_id, stores):
        raise library_clean.Incomplete({'faces': 1})
    monkeypatch.setattr(library_clean, 'run', incomplete)

    with pytest.raises(library_clean.Incomplete):
        app._dedicated_library_clean(LIB)
    assert events == ['begin']  # cleanupState stays 'cleaning' until a retry succeeds


def test_dispatcher_honours_the_rollback_setting(monkeypatch, unfenced):
    seen = []
    monkeypatch.setattr(app, '_legacy_library_clean', lambda library_id: seen.append('legacy') or {})
    monkeypatch.setattr(app, '_dedicated_library_clean', lambda library_id: seen.append('dedicated') or {})
    for engine in ('legacy', 'dedicated'):
        monkeypatch.setattr(app, 'LIBRARY_CLEAN_ENGINE', engine)
        app._execute_library_clean(LIB)
    assert seen == ['legacy', 'dedicated']


def test_progress_updates_the_existing_job_row_only(monkeypatch):
    jobs = Table()
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    jobs.upsert_entity({'PartitionKey': LIB, 'RowKey': 'libclean:job', 'jobType': 'library_clean'})
    token = app._LIBRARY_CLEAN_JOB.set('libclean:job')
    try:
        app._library_clean_stores(LIB, None).progress({'phase': 'phase2', 'photos': 7})
    finally:
        app._LIBRARY_CLEAN_JOB.reset(token)
    assert '"photos":7' in jobs.rows[(LIB, 'libclean:job')]['progress']
    assert jobs.rows[(LIB, 'libclean:job')]['jobType'] == 'library_clean'

    token = app._LIBRARY_CLEAN_JOB.set('libclean:missing')
    try:
        app._library_clean_stores(LIB, None).progress({'phase': 'phase2'})
    finally:
        app._LIBRARY_CLEAN_JOB.reset(token)
    assert (LIB, 'libclean:missing') not in jobs.rows


# --- the batched wave fence (processing_cleanup.begin_library_cleanup) -------------------------------

def test_begin_cleanup_writes_proofs_and_metadata_in_transactions():
    from test_wave_cleanup_live import world as wave_world
    import processing_cleanup
    table, metadata, store, _ = wave_world()
    for i in range(250):
        metadata.create_entity(dict(PartitionKey='u', RowKey=f'p{i}.jpg', anonymousImageId=f'a{i}',
                                    processingRevision='r', processingFence=f'attempt{i}', exifData='x' * 100))
    proof_commits_before = len(table.transactions)

    processing_cleanup.begin_library_cleanup(table, metadata, 'u')

    proofs = [row for key, row in table.rows.items() if key[1].startswith('deleted-source!')]
    assert len(proofs) == 250
    assert {row['processingAttempt'] for row in proofs} == {f'attempt{i}' for i in range(250)}
    assert all(metadata.get_entity('u', f'p{i}.jpg')['processing_state'] == 'deleted' for i in range(250))
    proof_commits = [ops for ops in table.transactions[proof_commits_before:]
                     if any(op[1]['RowKey'].startswith('deleted-source!') for op in ops)]
    assert len(proof_commits) == 3  # 96 + 96 + 58, not 250
    metadata_batches = [ops for ops in metadata.transactions if len(ops) > 1]
    assert [len(ops) for ops in metadata_batches] == [100, 100, 50]
    # Rerunning it (a retried clean) writes no duplicate proofs.
    processing_cleanup.begin_library_cleanup(table, metadata, 'u')
    assert len([key for key in table.rows if key[1].startswith('deleted-source!')]) == 250


# --- the cheaper fence proof ---------------------------------------------------------------------------

def test_publication_claims_are_found_by_library_in_two_queries(monkeypatch):
    import automatic_index_updates as automatic
    import time as _time
    from index_work import PARTITION
    jobs = Table()
    monkeypatch.setattr(app, 'jobs_table_client', jobs)
    queries = []
    real_query = jobs.query_entities
    monkeypatch.setattr(jobs, 'query_entities', lambda q, **kw: queries.append(q) or real_query(q, **kw))
    jobs.upsert_entity({'PartitionKey': PARTITION, 'RowKey': 'k1', 'libraryId': OTHER,
                        'leaseUntil': _time.time() + 60})

    automatic._check_publication_claims(LIB)  # another library's live claim does not block this one
    assert len(queries) == 2

    jobs.upsert_entity({'PartitionKey': PARTITION, 'RowKey': 'k2', 'libraryId': LIB,
                        'leaseUntil': _time.time() + 60})
    with pytest.raises(automatic.SourceBusy):
        automatic._check_publication_claims(LIB)
    jobs.upsert_entity({'PartitionKey': PARTITION, 'RowKey': 'k2', 'libraryId': LIB,
                        'leaseUntil': _time.time() - 1})
    automatic._check_publication_claims(LIB)  # an expired claim does not
