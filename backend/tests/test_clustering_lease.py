"""Lease SDK contract and deterministic loss tests; no credentials or sleeps."""
import hashlib
import json
import threading
import uuid

from azure.core.exceptions import (
    HttpResponseError, ResourceExistsError, ResourceNotFoundError,
    ServiceRequestError, ServiceResponseError,
)
import pytest

import clustering_lease as module
from clustering_lease import (
    BlobLibraryLeaseFactory, RetryableLibraryLeaseError,
    mark_library_changed, read_source_revision,
)


class FakeLease:
    def __init__(self, blob):
        self.blob = blob
        self.renew_error = None
        self.release_error = None
        self.renew_callback = None
        self.renewals = 0
        self.releases = 0

    def renew(self):
        self.renewals += 1
        if self.renew_callback:
            self.renew_callback()
        if self.renew_error:
            raise self.renew_error

    def release(self):
        self.releases += 1
        self.blob.active = None
        if self.release_error:
            raise self.release_error


class FakeBlob:
    def __init__(self):
        self.raw = None
        self.active = None
        self.leases = []
        self.uploads = []
        self.downloads = []
        self.durations = []
        self.create_error = self.acquire_error = None
        self.read_error = self.publish_error = None
        self.readall_error = None

    def upload_blob(self, data, *, overwrite, lease=None):
        self.uploads.append((json.loads(data), overwrite, lease))
        if not overwrite:
            if self.create_error:
                raise self.create_error
            if self.raw is not None:
                raise ResourceExistsError('exists')
            assert lease is None
        else:
            assert lease is self.active
            if self.publish_error:
                raise self.publish_error
        self.raw = data.encode('utf-8')

    def acquire_lease(self, *, lease_duration):
        self.durations.append(lease_duration)
        if self.acquire_error:
            raise self.acquire_error
        if self.active is not None:
            error = ResourceExistsError('LeaseAlreadyPresent')
            error.status_code = 409
            raise error
        self.active = FakeLease(self)
        self.leases.append(self.active)
        return self.active

    def download_blob(self, *, lease=None, offset, length):
        assert lease is self.active
        self.downloads.append((lease, offset, length))
        if self.read_error:
            raise self.read_error
        if self.raw is None:
            raise ResourceNotFoundError('missing blob')
        blob = self

        class Download:
            def readall(self):
                if blob.readall_error:
                    raise blob.readall_error
                return blob.raw[:length]

        return Download()


class FakeService:
    def __init__(self):
        self.blobs = {}
        self.calls = []

    def get_blob_client(self, *, container, blob):
        self.calls.append((container, blob))
        return self.blobs.setdefault((container, blob), FakeBlob())


@pytest.fixture
def setup():
    client = FakeService()
    factory = BlobLibraryLeaseFactory(client, 'existing-container')
    name = 'faiss-assignment-locks/' + hashlib.sha256(b'library').hexdigest() + '.json'
    blob = client.get_blob_client(container='existing-container', blob=name)
    client.calls.clear()
    return client, factory, blob


def test_sdk_contract_initial_state_generation_and_closed_guard(setup):
    client, factory, blob = setup
    with factory('library') as guard:
        guard.check()
        generation = guard.cache_generation
        assert isinstance(generation, tuple) and len(generation) == 2
        assert str(uuid.UUID(generation[0])) == generation[0]
        assert generation[1] == ''
        initial, overwrite, lease = blob.uploads[0]
        assert initial['owner'] == '' and uuid.UUID(initial['generation'])
        assert overwrite is False and lease is None
        assert blob.uploads[1] == ({'owner': factory._owner, 'generation': generation[0]}, True, blob.active)
        assert blob.durations == [60]
        assert blob.downloads == [(blob.active, 0, 4097)]
        digest = hashlib.sha256(b'library').hexdigest() + '.json'
        assert client.calls == [
            ('existing-container', 'faiss-assignment-locks/' + digest),
            ('existing-container', 'faiss-source-revisions/' + digest),
        ]
    assert blob.leases[0].releases == 1
    assert not guard._thread.is_alive()
    with pytest.raises(RetryableLibraryLeaseError, match='closed'):
        guard.check()


def test_same_owner_stable_transfer_and_return_change_generation(setup):
    client, first, blob = setup
    second = BlobLibraryLeaseFactory(client, 'existing-container')
    generations = []
    for factory in (first, first, second, second, first):
        with factory('library') as guard:
            generations.append(guard.cache_generation)
    assert generations[0] == generations[1]
    assert generations[2] == generations[3]
    assert len({generations[0], generations[2], generations[4]}) == 3
    assert all(lease.releases == 1 for lease in blob.leases)


def source_blob(client, user='library'):
    name = 'faiss-source-revisions/' + hashlib.sha256(user.encode()).hexdigest() + '.json'
    return client.get_blob_client(container='existing-container', blob=name)


def test_missing_source_revision_does_not_create_blob(setup):
    client, _, _ = setup
    assert read_source_revision(client, 'existing-container', 'library') == ''
    blob = source_blob(client)
    assert blob.raw is None and not blob.uploads
    assert blob.downloads == [(None, 0, 4097)]


def test_mark_overwrites_with_unique_strict_uuid_and_roundtrips(setup):
    client, _, _ = setup
    revisions = []
    for _ in range(2):
        mark_library_changed(client, 'existing-container', 'library')
        revisions.append(read_source_revision(client, 'existing-container', 'library'))
    assert revisions[0] != revisions[1]
    blob = source_blob(client)
    assert blob.uploads == [({'revision': revision}, True, None) for revision in revisions]
    assert all(str(uuid.UUID(revision)) == revision for revision in revisions)


def test_same_owner_revision_updates_change_generation_unchanged_reuses(setup):
    client, factory, lock_blob = setup
    generations = []
    for changed in (False, False, True, False, True):
        if changed:
            mark_library_changed(client, 'existing-container', 'library')
        with factory('library') as guard:
            generations.append(guard.cache_generation)
    assert generations[0] == generations[1]
    assert generations[2] == generations[3]
    assert len(set(generations)) == 3
    assert len({generation[0] for generation in generations}) == 1
    assert generations[0][1] == ''
    assert json.loads(lock_blob.raw)['owner'] == factory._owner


def test_guard_checks_do_not_remotely_read_source_revision(setup):
    client, factory, _ = setup
    mark_library_changed(client, 'existing-container', 'library')
    blob = source_blob(client)
    with factory('library') as guard:
        generation = guard.cache_generation
        mark_library_changed(client, 'existing-container', 'library')
        blob.read_error = ServiceRequestError('checks must remain local')
        for _ in range(3):
            guard.check()
        assert guard.cache_generation == generation
        assert blob.downloads == [(None, 0, 4097)]
    blob.read_error = None
    with factory('library') as guard:
        assert guard.cache_generation[0] == generation[0]
        assert guard.cache_generation[1] != generation[1]


@pytest.mark.parametrize('stage', ['read', 'readall'])
@pytest.mark.parametrize('error_type', [ServiceRequestError, ServiceResponseError, HttpResponseError])
def test_source_read_failures_propagate_and_factory_releases(setup, stage, error_type):
    client, factory, lock_blob = setup
    mark_library_changed(client, 'existing-container', 'library')
    error = error_type('revision unavailable')
    setattr(source_blob(client), stage + '_error', error)
    with pytest.raises(error_type) as caught:
        read_source_revision(client, 'existing-container', 'library')
    assert caught.value is error
    with pytest.raises(error_type) as caught:
        with factory('library'):
            pytest.fail('unreadable source revision yielded')
    assert caught.value is error
    assert lock_blob.leases[0].releases == 1


@pytest.mark.parametrize('raw', [
    b'', b'not json', b'\xff', b'[]', b'{}', b'null',
    b'{"revision":""}', b'{"revision":false}', b'{"revision":123}',
    b'{"revision":"not-a-uuid"}',
    json.dumps({'revision': str(uuid.uuid4()), 'unexpected': 1}).encode(),
    ('{"revision":"bad","revision":"' + str(uuid.uuid4()) + '"}').encode(),
    json.dumps({'revision': uuid.uuid4().hex}).encode(),
    b' ' * 4097,
])
def test_corrupt_source_revision_fails_closed_and_factory_releases(setup, raw):
    client, factory, lock_blob = setup
    blob = source_blob(client)
    blob.raw = raw
    with pytest.raises(RetryableLibraryLeaseError, match='Invalid library source revision'):
        read_source_revision(client, 'existing-container', 'library')
    with pytest.raises(RetryableLibraryLeaseError, match='Invalid library source revision'):
        with factory('library'):
            pytest.fail('corrupt source revision yielded')
    assert blob.raw == raw and not blob.uploads
    assert lock_blob.leases[0].releases == 1


@pytest.mark.parametrize('error_type', [
    ServiceRequestError, ServiceResponseError, HttpResponseError, ResourceNotFoundError,
])
def test_mark_errors_propagate_without_replacing_revision(setup, error_type):
    client, _, _ = setup
    mark_library_changed(client, 'existing-container', 'library')
    blob = source_blob(client)
    previous = blob.raw
    error = error_type('publication unavailable')
    blob.publish_error = error
    with pytest.raises(error_type) as caught:
        mark_library_changed(client, 'existing-container', 'library')
    assert caught.value is error
    assert blob.raw == previous


@pytest.mark.parametrize('helper', [mark_library_changed, read_source_revision])
@pytest.mark.parametrize('container,user', [('', 'library'), (None, 'library'),
                                         ('existing-container', ''),
                                         ('existing-container', None),
                                         ('existing-container', 123)])
def test_source_helpers_invalid_inputs_have_no_sdk_calls(setup, helper, container, user):
    client, _, _ = setup
    with pytest.raises(ValueError):
        helper(client, container, user)
    assert not client.calls


def test_unicode_library_ids_isolated_and_hashed(setup):
    client, factory, _ = setup
    for library in ('../private/name', '雪', 'other'):
        with factory(library) as guard:
            guard.check()
        digest = hashlib.sha256(library.encode()).hexdigest() + '.json'
        assert client.calls[-2][1] == 'faiss-assignment-locks/' + digest
        assert client.calls[-1][1] == 'faiss-source-revisions/' + digest
    assert len(set(client.calls)) == 6


def test_contention_is_immediate_and_does_not_release_other_owner(setup):
    client, factory, blob = setup
    other = BlobLibraryLeaseFactory(client, 'existing-container')
    with factory('library') as guard:
        with pytest.raises(RetryableLibraryLeaseError) as caught:
            with other('library'):
                pytest.fail('conflicting acquisition yielded')
        assert isinstance(caught.value.__cause__, ResourceExistsError)
        assert blob.leases[0].releases == 0
        guard.check()
    assert blob.leases[0].releases == 1


@pytest.mark.parametrize('status', [409, 412, 403, 404, 500])
def test_acquisition_sdk_status_handling(setup, status):
    _, factory, blob = setup
    error = HttpResponseError('acquire')
    error.status_code = status
    blob.acquire_error = error
    expected = RetryableLibraryLeaseError if status in (409, 412) else HttpResponseError
    with pytest.raises(expected) as caught:
        with factory('library'):
            pytest.fail('acquisition yielded')
    assert (caught.value.__cause__ if status in (409, 412) else caught.value) is error
    assert not blob.leases


@pytest.mark.parametrize('stage', ['create', 'acquire', 'read', 'publish'])
@pytest.mark.parametrize('error_type', [ServiceRequestError, ServiceResponseError, ResourceNotFoundError])
def test_transport_and_missing_resource_errors_propagate(setup, stage, error_type):
    _, factory, blob = setup
    error = error_type('storage unavailable')
    setattr(blob, stage + '_error', error)
    with pytest.raises(error_type) as caught:
        with factory('library'):
            pytest.fail('failed setup yielded')
    assert caught.value is error
    if stage in ('read', 'publish'):
        assert blob.leases[0].releases == 1
    else:
        assert not blob.leases


@pytest.mark.parametrize('raw', [
    b'', b'not json', b'\xff', b'[]', b'{}', b'null',
    b'{"owner":"","generation":""}',
    b'{"owner":false,"generation":"bad"}',
    json.dumps({'owner': 'not-a-uuid', 'generation': str(uuid.uuid4())}).encode(),
    json.dumps({'owner': '', 'generation': 123}).encode(),
    json.dumps({'owner': '', 'generation': str(uuid.uuid4()), 'unexpected': 1}).encode(),
    ('{"owner":"bad","owner":"","generation":"' + str(uuid.uuid4()) + '"}').encode(),
    b' ' * 4097,
])
def test_corrupt_state_fails_closed_without_overwrite_and_releases(setup, raw):
    _, factory, blob = setup
    blob.raw = raw
    with pytest.raises(RetryableLibraryLeaseError, match='Invalid library lease state'):
        with factory('library'):
            pytest.fail('corrupt state yielded')
    assert blob.raw == raw
    assert all(not overwrite for _, overwrite, _ in blob.uploads)
    assert blob.leases[0].releases == 1


def test_expiry_boundary_and_never_resurrects(setup, monkeypatch):
    _, factory, blob = setup
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    with factory('library') as guard:
        now[0] = 159.999
        guard.check()
        now[0] = 160
        with pytest.raises(RetryableLibraryLeaseError):
            guard.check()
        now[0] = 101
        guard._renew()
        with pytest.raises(RetryableLibraryLeaseError):
            guard.check()
        assert blob.active.renewals == 0


def test_slow_acquisition_fails_before_state_publication(setup, monkeypatch):
    _, factory, blob = setup
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    acquire = blob.acquire_lease

    def slow_acquire(**kwargs):
        lease = acquire(**kwargs)
        now[0] = 160
        return lease

    monkeypatch.setattr(blob, 'acquire_lease', slow_acquire)
    with pytest.raises(RetryableLibraryLeaseError):
        with factory('library'):
            pytest.fail('expired acquisition yielded')
    assert blob.leases[0].releases == 1
    assert all(not overwrite for _, overwrite, _ in blob.uploads)


def test_thread_start_failure_releases_lease(setup, monkeypatch):
    _, factory, blob = setup
    error = RuntimeError('cannot start thread')

    def fail_start(thread):
        raise error

    monkeypatch.setattr(module.threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError) as caught:
        with factory('library'):
            pytest.fail('failed worker startup yielded')
    assert caught.value is error
    assert blob.leases[0].releases == 1


def test_successful_renewal_extends_from_request_start(setup, monkeypatch):
    _, factory, blob = setup
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    with factory('library') as guard:
        now[0] = 115
        blob.active.renew_callback = lambda: now.__setitem__(0, 120)
        guard._renew()
        assert guard._last_renewed == 115
        now[0] = 174.99
        guard.check()
        now[0] = 175
        with pytest.raises(RetryableLibraryLeaseError):
            guard.check()


def test_slow_renewal_cannot_resurrect_expired_guard(setup, monkeypatch):
    _, factory, blob = setup
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    with factory('library') as guard:
        now[0] = 115
        blob.active.renew_callback = lambda: now.__setitem__(0, 160)
        guard._renew()
        with pytest.raises(RetryableLibraryLeaseError):
            guard.check()


@pytest.mark.parametrize('error', [ServiceRequestError('offline'), HttpResponseError('lease lost')])
def test_renewal_failure_permanently_blocks_next_write(setup, error):
    _, factory, blob = setup
    with factory('library') as guard:
        blob.active.renew_error = error
        guard._renew()
        with pytest.raises(RetryableLibraryLeaseError) as caught:
            guard.check()
        assert caught.value.__cause__ is error
        blob.active.renew_error = None
        guard._renew()
        assert blob.active.renewals == 1
    assert blob.leases[0].releases == 1


def test_background_renewal_uses_event_wait_and_stops_on_failure(setup):
    client, _, blob = setup
    factory = BlobLibraryLeaseFactory(client, 'existing-container', renew_seconds=0.001)
    with factory('library') as guard:
        blob.active.renew_error = ServiceRequestError('offline')
        assert guard._stop.wait(timeout=2), 'renewal worker did not report loss'
        with pytest.raises(RetryableLibraryLeaseError):
            guard.check()
    assert not guard._thread.is_alive()


def test_checks_not_blocked_by_stuck_renew_and_join_is_bounded(setup, monkeypatch):
    _, factory, blob = setup
    now = [100.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    entered, unblock = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert unblock.wait(timeout=5)

    try:
        with factory('library') as guard:
            blob.active.renew_callback = blocked
            # Wake the real worker without sleeping or polling.
            guard._stop.set()
            guard._thread.join(timeout=2)
            guard._stop.clear()
            guard._thread = threading.Thread(target=guard._renew, daemon=True)
            guard._thread.start()
            assert entered.wait(timeout=2)
            now[0] = 160
            with pytest.raises(RetryableLibraryLeaseError):
                guard.check()
        assert guard._thread.is_alive()  # bounded join did not wait for network I/O
        assert blob.leases[0].releases == 1
    finally:
        unblock.set()
        guard._thread.join(timeout=2)
    assert not guard._thread.is_alive()


@pytest.mark.parametrize('primary', [False, True])
def test_release_failure_logged_without_masking_primary(setup, caplog, primary):
    _, factory, blob = setup
    error = RuntimeError('build failed')
    try:
        with factory('library'):
            blob.active.release_error = ServiceRequestError('release failed')
            if primary:
                raise error
    except RuntimeError as caught:
        assert primary and caught is error
    else:
        assert not primary
    assert 'Failed to release library assignment lease' in caplog.text
    assert blob.leases[0].releases == 1


@pytest.mark.parametrize('kwargs', [
    {'duration': 14}, {'duration': 61}, {'duration': -1}, {'duration': True},
    {'duration': 60.0}, {'renew_seconds': 0}, {'renew_seconds': -1},
    {'renew_seconds': 60}, {'renew_seconds': float('nan')},
    {'renew_seconds': float('inf')}, {'renew_seconds': True},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        BlobLibraryLeaseFactory(FakeService(), 'existing', **kwargs)


@pytest.mark.parametrize('user', ['', None, 123])
def test_invalid_library_id_has_no_sdk_calls(setup, user):
    client, factory, _ = setup
    with pytest.raises(ValueError):
        with factory(user):
            pytest.fail('invalid ID yielded')
    assert not client.calls


def test_faiss_build_exception_releases_production_injected_lease(setup, monkeypatch):
    pytest.importorskip('faiss')
    import faiss_assignment as assignment
    from test_faiss_assignment import Harness

    _, factory, blob = setup
    assignment.invalidate()
    h = Harness()
    h.assigner.lease = factory
    h.face('new', user='library')
    error = RuntimeError('index build failed')

    def fail_build(*args):
        raise error

    monkeypatch.setattr(assignment.LiveFaceIndex, 'build', fail_build)
    try:
        with pytest.raises(RuntimeError) as caught:
            h.assign('new', user='library')
        assert caught.value is error
        assert blob.leases[0].releases == 1
        assert not h.people.rows
    finally:
        assignment.invalidate()