"""Failure-path integration tests for the real relay and the Vision/cluster/
index consumer entry points in upload_wave_execution.py -- see
docs/indexing-clustering-target-state.md step 3 part 7. Exercises the real
ShadowWaves/WorkStore/ExecutionLeases together, not mocks of each other.
"""
import json

import pytest
from azure.core.exceptions import ResourceNotFoundError

from clustering_wave import ClusteringJournal
from execution_lease import ExecutionLeases, OwnershipLost
from upload_waves import ShadowWaves, digest
from upload_wave_execution import (
    claim_release, dispatch_outbox, release_and_relay, run_cluster_release,
    run_index_release, run_vision_release, stage_work_store,
)
from wave_publication import ObligationLedger
from tests.test_index_work import EtagTable
from tests.test_upload_waves import AtomicTable, close, finish_scan, register_accepted


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    table = AtomicTable()
    work_table = EtagTable()
    lease_table = EtagTable()
    store = ShadowWaves(table, 'library-A')
    work = stage_work_store(work_table)
    leases = ExecutionLeases(table, partition=store.partition)
    return store, work, leases, table, work_table


class FakeMetadata:
    def __init__(self):
        self.rows = {}

    def put(self, library, filename, **fields):
        self.rows[(library, filename)] = dict(fields)

    def get_entity(self, library, filename):
        try:
            return dict(self.rows[(library, filename)])
        except KeyError:
            raise ResourceNotFoundError('missing')


def settled_row(photo, revision, *, face_count=0, filename='original.jpg'):
    from processing_generation import allocate_generation
    from processing_completion import STEPS
    return dict(PartitionKey='library-A', RowKey=filename, anonymousImageId=photo,
        fileHash='a'*64, faceCount=face_count,
        **allocate_generation(revision, photo_id=photo, file_hash='a'*64, is_video=filename.endswith('.mp4')),
        **{step + '_status': 'no_data' for step in STEPS})


def register_accepted(store):
    from processing_generation import REQUIRED_STEPS
    row = store.open_session('parent', 1)
    store.register('parent', [{'itemId': 'item-1', 'filename': 'original.jpg'}])
    resolved = store.resolve('parent', 'item-1', outcome='accepted', photo_id='photo-1',
        processing_revision='revision-1', filename='original.jpg', required_steps=REQUIRED_STEPS)
    return row['waveId'], resolved


def publish_completion(store, metadata, *, faces=(), filename='original.jpg'):
    from processing_completion import prepare_completion
    from processing_receipts import ArtifactReceipts
    row = metadata.rows[store.library, filename]
    receipts = ArtifactReceipts(store.table, store.library)
    snapshots = []
    for face in faces:
        snapshots.append(receipts._put('face', face))
    payload = dict(schemaVersion=1, library=store.library, filename=row['RowKey'],
        photoId=row['anonymousImageId'], revision=row['processingRevision'],
        statuses={step:row[step+'_status'] for step in
            ('preview','thumbnail','exif','ocr','ai_vision','map_detection','face')},
        derivatives={}, faceCount=len(faces), faceSnapshots=snapshots, faceLookupGeneration='lookup')
    row['faceCount'] = len(faces)
    row['artifactReceipt'] = json.dumps(dict(id=receipts._put('manifest',payload), library=store.library,
        filename=row['RowKey'],photoId=row['anonymousImageId'],revision=row['processingRevision']))
    prepare_completion(row)
    intent = json.loads(row['processingCompletionOutbox'])[-1]
    store.accept_source_completion(intent)
    return intent


def no_faces(library, filename):
    return []


class FakeAssigner:
    def __init__(self):
        self.calls = []

    def batch(self, user, ids, staged=True):
        calls = self.calls

        class Ctx:
            def __enter__(self):
                def assign(filename, face_ids):
                    calls.append((filename, list(face_ids)))
                return assign

            def __exit__(self, *exc):
                return False
        return Ctx()


def face_row(filename, revision='rev-1'):
    return dict(filename=filename, embedding='[1.0, 2.0]', embeddingVersion='v1',
                alignmentMethod='a', processingRevision=revision)


# --- Part 2/7: durable release -> real queue delivery, crash/duplicate tolerance ---

def test_crash_after_outbox_commit_before_send_is_retried_by_recover(world, monkeypatch):
    store, _unused_work, leases, table, work_table = world
    now = [1000.0]
    work = stage_work_store(work_table, clock=lambda: now[0])
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']

    def crash(*args):
        raise OSError('simulated send failure')
    # The durable WorkStore request (dispatch_outbox) lands before the send
    # is even attempted; a crash/exception IN the send itself propagates to
    # this caller (its queue client decides whether to retry this exact
    # call), but the durable request it already made survives regardless.
    with pytest.raises(OSError):
        release_and_relay(work, store, 'library-A', vision_key, 'vision', crash)
    assert work_table.rows  # Durable intent survives the failed send attempt.
    now[0] += 120  # Past both the lease and the (never-reserved) wake window.
    sent = []
    assert work.recover(lambda *args: sent.append(args)) == 1


def test_lost_ack_does_not_trigger_a_duplicate_send_within_the_reservation_window(world):
    store, work, leases, table, work_table = world
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']
    sent = []
    assert release_and_relay(work, store, 'library-A', vision_key, 'vision', lambda *a: sent.append(a)) is True
    # A second relay call before the reservation window expires (e.g. a
    # concurrent delivery retry after an ack the first send never saw) must
    # not call send() again.
    assert release_and_relay(work, store, 'library-A', vision_key, 'vision', lambda *a: sent.append(a)) is False
    assert len(sent) == 1


def test_disabled_flag_prevents_runnable_dispatch_through_the_relay(world, monkeypatch):
    store, work, leases, table, work_table = world
    monkeypatch.delenv('UPLOAD_WAVE_EXECUTION_ENABLED', raising=False)
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']
    sent = []
    assert release_and_relay(work, store, 'library-A', vision_key, 'vision', lambda *a: sent.append(a)) is False
    assert not sent and not work_table.rows


# --- Part 3/7: renewable ownership at the consumer boundary ---

def test_duplicate_or_obsolete_vision_release_is_a_tolerated_noop(world):
    store, work, leases, table, work_table = world
    row = store.open_session('p', 0)
    close(store, 'p', row)
    old_key = store.status(row['waveId'])['visionPendingKey']
    store.open_session('later', 0)  # Reopens intake under the old release.
    ownership, wave = claim_release(leases, store, 'library-A', row['waveId'], 'vision', old_key, 'vision')
    assert ownership is None and wave['state'] == 'collecting'


def test_a_claimed_vision_stage_rejects_a_concurrent_claim(world):
    store, work, leases, table, work_table = world
    row = store.open_session('p', 0)
    close(store, 'p', row)
    key = store.status(row['waveId'])['visionPendingKey']
    claim_release(leases, store, 'library-A', row['waveId'], 'vision', key, 'vision')
    with pytest.raises(OwnershipLost, match='already claimed'):
        claim_release(leases, store, 'library-A', row['waveId'], 'vision', key, 'vision')


# --- Part 4/7: Vision consumer -- no-work barrier advancement + durable cluster handoff ---

def test_novalue_wave_advances_through_vision_and_durably_relays_the_cluster_release(world):
    """A wave with zero frozen members (no work at all) must still advance
    the barrier using its own closed-session witness and durably produce
    the cluster release in the same call -- no per-photo work needed to
    wake up and nothing extra waits on a worker that was never dispatched."""
    store, work, leases, table, work_table = world
    row = store.open_session('p', 0)
    close(store, 'p', row)
    vision_key = store.status(row['waveId'])['visionPendingKey']
    metadata = FakeMetadata()
    cluster_sent = []
    wave = run_vision_release(leases, work, store, 'library-A', row['waveId'], vision_key,
                              metadata, None, no_faces, lambda *a: cluster_sent.append(a))
    assert wave['state'] == 'ready'
    assert cluster_sent  # Vision's own completion durably produced the cluster release.
    cluster_key = 'shadow-cluster!' + row['waveId']
    assert store._get(cluster_key)['dispatchable'] is True


def test_vision_consumer_persists_progress_and_releases_without_completing_while_members_remain_pending(world):
    store, work, leases, table, work_table = world
    wave_id, resolved = register_accepted(store)
    close(store, 'parent', resolved)
    vision_key = store.status(wave_id)['visionPendingKey']
    metadata = FakeMetadata()  # No row for 'original.jpg' -- source not finalized yet.
    cluster_sent = []
    wave = run_vision_release(leases, work, store, 'library-A', wave_id, vision_key,
                              metadata, None, no_faces, lambda *a: cluster_sent.append(a))
    assert wave['state'] == 'processing'
    assert not cluster_sent
    # The lease was released (not left dangling as 'running'), so a later
    # wakeup can reclaim it once the source actually settles.
    reclaimed = leases.claim('library-A', wave_id, 'vision', f'{wave_id}:1')
    assert reclaimed.epoch == 2


def test_vision_consumer_completes_once_the_source_settles_on_a_later_attempt(world):
    store, work, leases, table, work_table = world
    wave_id, resolved = register_accepted(store)
    close(store, 'parent', resolved)
    vision_key = store.status(wave_id)['visionPendingKey']
    metadata = FakeMetadata()
    metadata.put('library-A', 'original.jpg', **settled_row('photo-1', 'revision-1'))
    publish_completion(store, metadata)
    cluster_sent = []
    wave = run_vision_release(leases, work, store, 'library-A', wave_id, vision_key,
                              metadata, None, no_faces, lambda *a: cluster_sent.append(a))
    assert wave['state'] == 'ready'
    assert cluster_sent


# --- Part 5/7: cluster consumer -- immutable input validation, never builds serving indexes ---


def test_pending_vision_resumes_through_the_same_release(world):
    store, work, leases, *_ = world
    wave_id, resolved = register_accepted(store); close(store, 'parent', resolved)
    key = store.status(wave_id)['visionPendingKey']
    metadata = FakeMetadata()
    assert run_vision_release(leases, work, store, store.library, wave_id, key,
        metadata, None, no_faces, lambda *a: None)['state'] == 'processing'
    metadata.put(store.library, 'original.jpg', **settled_row('photo-1', 'revision-1'))
    publish_completion(store, metadata)
    assert run_vision_release(leases, work, store, store.library, wave_id, key,
        metadata, None, no_faces, lambda *a: None)['state'] == 'ready'


def test_unqualified_terminal_statuses_settle_as_failed_after_one_attempt(world):
    """Terminal-looking step statuses ('no_data') are not proof of success
    without a backing qualified completion/receipt. With the retry budget at
    one attempt, process_source succeeding without ever producing a
    completion settles this source as failed_exhausted immediately, rather
    than leaving it 'pending' forever waiting on an attempt that may never
    come -- the wave still reaches 'ready' (with a recorded failure) and
    releases to cluster exactly like any other fully-settled wave."""
    store, work, leases, *_ = world
    wave_id, resolved = register_accepted(store); close(store, 'parent', resolved)
    metadata = FakeMetadata()
    metadata.put(store.library, 'original.jpg', **settled_row('photo-1', 'revision-1'))
    called = []
    released = []
    wave = run_vision_release(leases, work, store, store.library, wave_id,
        store.status(wave_id)['visionPendingKey'], metadata, None, no_faces,
        lambda *a: released.append(a), process_source=lambda *a: called.append(a))
    assert called and wave['state'] == 'ready'
    assert store.status(wave_id)['failedMembers'] == 1
    assert released


def test_vision_calls_source_adapter_then_verifies_its_receipt(world):
    store, work, leases, *_ = world
    wave_id, resolved = register_accepted(store); close(store, 'parent', resolved)
    metadata = FakeMetadata()
    row = settled_row('photo-1', 'revision-1'); row['face_status'] = 'pending'
    metadata.put(store.library, 'original.jpg', **row)
    def process(library, filename, revision, renew):
        assert (library, filename, revision) == (store.library, 'original.jpg', 'revision-1')
        renew()
        metadata.rows[library, filename]['face_status'] = 'no_data'
        publish_completion(store, metadata)
    wave = run_vision_release(leases, work, store, store.library, wave_id,
        store.status(wave_id)['visionPendingKey'], metadata, None, no_faces,
        lambda *a: None, process_source=process)
    assert wave['state'] == 'ready'


def test_missing_source_adapter_rejects_work_instead_of_claiming_to_run_vision(world):
    store, work, leases, *_ = world
    wave_id, resolved = register_accepted(store); close(store, 'parent', resolved)
    metadata = FakeMetadata()
    metadata.put(store.library, 'original.jpg', **settled_row('photo-1', 'revision-1'))
    with pytest.raises(NotImplementedError, match='adapter'):
        run_vision_release(leases, work, store, store.library, wave_id,
            store.status(wave_id)['visionPendingKey'], metadata, None, no_faces, lambda *a: None)
    assert store.status(wave_id)['state'] != 'ready'


def ready_wave(world, *, with_face=False):
    from processing_generation import current_contract
    store, work, leases, *_ = world
    wave_id, resolved = register_accepted(store); close(store, 'parent', resolved)
    metadata = FakeMetadata()
    metadata.put(store.library, 'original.jpg', **settled_row('photo-1', 'revision-1'))
    faces = [dict(PartitionKey=store.library, RowKey='face-1', filename='original.jpg', sourcePhotoId='photo-1',
        alignmentMethod='5pt',
        processingRevision='revision-1', embedding='[1.0,2.0]',
        embeddingVersion=current_contract()['faceEmbeddingVersions'][0])] if with_face else []
    publish_completion(store, metadata, faces=faces)
    run_vision_release(leases, work, store, store.library, wave_id,
        store.status(wave_id)['visionPendingKey'], metadata, None, no_faces, lambda *a: None)
    return wave_id


def test_cluster_inputs_use_frozen_snapshots_and_missing_receipts_fail_closed(world):
    from upload_wave_execution import _load_wave_faces
    store, *_ = world
    wave_id = ready_wave(world, with_face=True)
    members, rows = _load_wave_faces(store, wave_id,
        face_table=object(), face_ids=lambda *a: pytest.fail('read mutable lookup'))
    assert members == {'original.jpg': ['face-1']}
    assert rows['face-1']['embedding'] == '[1.0,2.0]'
    key = next(key for key in store.table.rows if key[1].startswith('source-completion!'))
    del store.table.rows[key]
    with pytest.raises(ValueError, match='missing'):
        _load_wave_faces(store, wave_id)


def test_nonempty_cluster_does_not_call_unsafe_legacy_assigner(world):
    store, work, leases, *_ = world
    wave_id = ready_wave(world, with_face=True)
    assigner = FakeAssigner()
    with pytest.raises(TypeError, match='FaissAssigner'):
        run_cluster_release(leases, store, store.library, wave_id, 'shadow-cluster!' + wave_id,
            assigner=assigner, journal=None, obligations_table=EtagTable(), face_table=None, face_ids=None)
    assert not assigner.calls and store.status(wave_id)['state'] == 'ready'


def test_zero_face_cluster_commits_index_handoff_but_index_requires_publication(world):
    store, work, leases, *_ = world
    wave_id = ready_wave(world)
    obligations = EtagTable()
    wave = run_cluster_release(leases, store, store.library, wave_id, 'shadow-cluster!' + wave_id,
        assigner=real_wave_assigner().assigner, journal=None, obligations_table=obligations, face_table=None, face_ids=None)
    assert wave['state'] == 'indexing'
    ledger = ObligationLedger(obligations, wave_id, library=store.library, frozen_revision=wave['frozenRevision'],
        assignment_revision=wave['assignmentReceiptId'])
    result = run_index_release(leases, store, store.library, wave_id, wave['indexPendingKey'], ledger)
    assert result['state'] == 'indexing'


def test_disabling_execution_stops_prepared_relay_consumers_and_recovery(world, monkeypatch):
    from upload_wave_execution import recover_releases
    store, work, leases, *_ = world
    session = store.open_session('p', 0); close(store, 'p', session)
    key = store.status(session['waveId'])['visionPendingKey']
    dispatch_outbox(work, store, store.library, key, 'vision')
    monkeypatch.delenv('UPLOAD_WAVE_EXECUTION_ENABLED')
    send = lambda *a: pytest.fail('disabled send')
    assert not release_and_relay(work, store, store.library, key, 'vision', send)
    assert recover_releases(work, store, send) == 0
    assert run_vision_release(leases, work, store, store.library, session['waveId'], key,
        FakeMetadata(), None, no_faces, send) is None


def test_reopened_intake_stops_old_vision_work_before_barrier_mutation(world):
    store, work, leases, *_ = world
    wave_id, resolved = register_accepted(store); close(store, 'parent', resolved)
    metadata = FakeMetadata()
    metadata.put(store.library, 'original.jpg', **settled_row('photo-1', 'revision-1'))
    def reopen(*args):
        store.open_session('new-session', 0)
    with pytest.raises(OwnershipLost):
        run_vision_release(leases, work, store, store.library, wave_id,
            store.status(wave_id)['visionPendingKey'], metadata, None, no_faces,
            lambda *a: pytest.fail('old release'), process_source=reopen)
    assert store.status(wave_id)['state'] == 'collecting'


def test_wave_transition_is_conditioned_on_lease_etag_in_same_transaction(world):
    from execution_lease import execution_scope
    store, work, leases, table, *_ = world
    wave_id = ready_wave(world)
    key = 'shadow-cluster!' + wave_id
    ownership, _ = claim_release(leases, store, store.library, wave_id, 'cluster', key, 'cluster')
    now = [1000.0]
    leases.clock = lambda: now[0]
    # The original claim used wall-clock time. Move to that lease's expiry.
    now[0] = leases.row(ownership)['leaseUntil'] - 1
    def steal():
        now[0] += 2
        leases.claim(store.library, wave_id, 'cluster', ownership.inputRevision)
    table.before_commit = steal
    with execution_scope(leases, ownership, store, lambda: None):
        with pytest.raises(OwnershipLost):
            store.commit_assignment(wave_id, 'frontier', manifest_digest=store.status(wave_id)['manifestDigest'])
    assert store.status(wave_id)['state'] == 'ready'


def test_replacement_source_is_never_sent_to_the_old_wave_adapter(world):
    store, work, leases, *_ = world
    wave_id, resolved = register_accepted(store); close(store, 'parent', resolved)
    metadata = FakeMetadata()
    metadata.put(store.library, 'original.jpg', **settled_row('replacement-photo', 'new-revision'))
    wave = run_vision_release(leases, work, store, store.library, wave_id,
        store.status(wave_id)['visionPendingKey'], metadata, None, no_faces,
        lambda *a: pytest.fail('premature clustering'),
        process_source=lambda *a: pytest.fail('processed replacement for an old wave'))
    assert wave['state'] == 'processing'


def real_wave_assigner():
    from test_faiss_assignment import Harness
    harness = Harness()
    for attr, field in [('faces','face_table'), ('people','person_table'),
                        ('embeddings','embedding_table'), ('members','member_table')]:
        table = EtagTable()
        setattr(harness, attr, table)
        setattr(harness.assigner, field, table)
    return harness


def test_real_nonempty_cluster_executes_frozen_journal_and_commits_receipt(world):
    from upload_wave_execution import _load_wave_faces
    store, work, leases, *_ = world
    wave_id = ready_wave(world, with_face=True)
    h = real_wave_assigner()
    _, frozen = _load_wave_faces(store, wave_id)
    h.faces.create_entity(frozen['face-1'])
    result = run_cluster_release(leases, store, store.library, wave_id, 'shadow-cluster!'+wave_id,
        assigner=h.assigner, journal=None, obligations_table=EtagTable(), face_table=None, face_ids=None)
    assert result['state'] == 'indexing' and result['assignmentReceiptId']
    assert h.faces.rows[store.library, 'face-1']['personId']
    assert store._get(f'assignment!{wave_id}!complete')['digest'] == result['assignmentReceiptId']
    assert not h.metadata


def test_index_handoff_cannot_rebind_an_assignment_receipt(world):
    from upload_waves import Conflict
    store, work, leases, *_ = world
    wave_id = ready_wave(world)
    result = run_cluster_release(leases, store, store.library, wave_id, 'shadow-cluster!'+wave_id,
        assigner=real_wave_assigner().assigner, journal=None,
        obligations_table=EtagTable(), face_table=None, face_ids=None)
    with pytest.raises(Conflict, match='committed receipt'):
        store.commit_assignment(wave_id, result['requiredPublicationId'],
            manifest_digest=result['manifestDigest'], assignment_receipt='0'*64)


def test_index_dag_completes_only_after_every_exact_receipt(world):
    from wave_catalog import SCOPES, PREREQUISITES
    store, _, leases, *_ = world
    wave_id = ready_wave(world)
    table = EtagTable()
    wave = run_cluster_release(leases, store, store.library, wave_id, 'shadow-cluster!'+wave_id,
        assigner=real_wave_assigner().assigner, journal=None,
        obligations_table=table, face_table=None, face_ids=None)
    ledger = ObligationLedger(table, wave_id, library=store.library,
        frozen_revision=wave['frozenRevision'], assignment_revision=wave['assignmentReceiptId'])
    published=[]
    def publish(scope, obligation, check):
        check()
        assert set(PREREQUISITES.get(scope, ())) <= set(published)
        assert store.status(wave_id)['state'] == 'indexing'
        published.append(scope)
        return digest([scope, obligation])
    complete = run_index_release(leases, store, store.library, wave_id,
        wave['indexPendingKey'], ledger, publisher=publish)
    assert set(published) == set(SCOPES)
    assert complete['state'] == 'complete'
    assert ledger.completion_proof()['assignmentReceiptId'] == wave['assignmentReceiptId']


def test_failed_publisher_never_completes_wave(world):
    store, _, leases, *_ = world
    wave_id = ready_wave(world)
    table = EtagTable()
    wave = run_cluster_release(leases, store, store.library, wave_id, 'shadow-cluster!'+wave_id,
        assigner=real_wave_assigner().assigner, journal=None,
        obligations_table=table, face_table=None, face_ids=None)
    ledger = ObligationLedger(table, wave_id, library=store.library,
        frozen_revision=wave['frozenRevision'], assignment_revision=wave['assignmentReceiptId'])
    def failed(*args):raise OSError('publisher unavailable')
    with pytest.raises(OSError):
        run_index_release(leases, store, store.library, wave_id, wave['indexPendingKey'], ledger,publisher=failed)
    assert store.status(wave_id)['state'] == 'indexing'
    assert not ledger.is_complete()
