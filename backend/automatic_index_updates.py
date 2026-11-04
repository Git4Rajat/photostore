"""Automatic artifact coverage, separate from the legacy catalog dirty sets.

No new Azure tables: controls and versioned dirty IDs use searchdirty; wakeups
use the existing jobs WorkStore. A manifest is NOT proof of source coverage.
Only this ledger's CAS certification after publication establishes a base.

Proof: every source writer persists an active reservation and dirty IDs BEFORE
writing, and advances the revision again AFTER writing. A builder captures an
idle revision before reads and certifies that exact ETag after publication.
An overlapping writer either makes capture busy or defeats certification; no
dirty versions are acknowledged in either case. Blob/table commits are not
atomic: a losing build may leave a stale pointer, but it cannot certify it.
The next worker rejects that pointer as a base and performs a full build.
Reservations NEVER expire: a paused writer cannot become invisible by timing
out. An orphan after SIGKILL fails closed (requires operator quiescence/repair),
not an unsafe timeout-based acknowledgement. All writers must run this code.

Explicit admin recovery: POST /api/admin/indexes/source-reservations with
action=fence arms an indefinite library admission fence and returns fenceId.
STOP/drain ALL library writers (including upload, vision, clustering, legacy
processes and paused HTTP requests) before action=prove with that fenceId,
confirm=ALL_LIBRARY_WRITERS_STOPPED, allWritersStopped=true,
noPausedRequests=true, legacyWritersStopped=true and a nonempty reason. This
is operator proof, NOT inferred from expired leases, wave policy or active=0.
The real renewable catalog lock and live maintenance claims are also checked.
action=recover resets orphan reservations to fullRequired, preserves dirty IDs,
and persists every rebuild intent BEFORE changing coverage. action=resume
explicitly releases the fence; until then source writes/builders remain blocked.
Confirmed proof also permits existing library/account purge to remove immutable
artifacts and automatic state inside the same lock, before cleanup completion.
Leave writers stopped until recovery/purge finishes; proof has no timeout.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import logging
import os
import random
import time
import uuid

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from index_work import DirtySnapshot, WorkStore, PARTITION, acknowledge_dirty, etag
from index_publication import serialize_catalog

KINDS = ('vector', 'people_embedding', 'tag_embedding', 'lexical_artifact', 'hash')
CONTROL = '!coverage'
VERSION = 1
LIMIT = 512
TAG_PARTITION = 'index-model-tag-pending'
QUIESCENCE_PARTITION = 'index-operator-quiescence'
SCOPES = {'hash': 'dedup', 'lexical_artifact': 'lexical_artifact'}
LOG = logging.getLogger(__name__)
_QUIESCED_CLEANUP = ContextVar('quiesced_index_source_cleanup', default=None)


@contextmanager
def quiesced_source_cleanup(user, check):
    check()
    token = _QUIESCED_CLEANUP.set((user, check))
    try:
        yield
        check()
    finally:
        _QUIESCED_CLEANUP.reset(token)


def current_cleanup_check(user):
    active = _QUIESCED_CLEANUP.get()
    return active[1] if active is not None and active[0] == user else None


def quiesced_cleanup(function):
    @wraps(function)
    def wrapped(user, *args, **kwargs):
        import storage_utils as su
        if not su.DURABLE_INDEX_UPDATES:
            return function(user, *args, **kwargs)
        from index_artifact_cleanup import artifact_catalog_serialization
        with artifact_catalog_serialization(user, storage=su) as guard:
            check = quiescence_check(user, guard=guard)
            with quiesced_source_cleanup(user, check):
                return function(user, *args, **kwargs)
    return wrapped


def _quiescence_row(user):
    import app
    if app.jobs_table_client is None:
        return None
    try:
        return app.jobs_table_client.get_entity(QUIESCENCE_PARTITION, hashlib.sha256(user.encode()).hexdigest())
    except ResourceNotFoundError:
        return None


def check_source_admission(user):
    """Indefinite operator fence; never confuse an expired lease with a drain."""
    import storage_utils as su
    if su.DURABLE_INDEX_UPDATES and _quiescence_row(user) is not None:
        raise SourceBusy('Library writers fenced for operator quiescence')


def _check_publication_claims(user):
    import app
    from index_work import PARTITION
    from index_updates import SCOPE_KINDS
    if app.jobs_table_client is None:
        raise RuntimeError('Quiescence work storage unavailable')
    for pk in (PARTITION, TAG_PARTITION):
        store = WorkStore(app.jobs_table_client, partition=pk)
        for scope in SCOPE_KINDS:
            row = store._get(store.key(user, scope))
            if row and float(row.get('leaseUntil') or 0) > time.time():
                raise SourceBusy('Live maintenance claim prevents quiesced recovery')


def operator_fence(user, *, operator, reason):
    import app
    if not operator or not reason or app.jobs_table_client is None:
        raise ValueError('Authenticated operator, reason and jobs storage required')
    existing = _quiescence_row(user)
    if existing is None:
        row = dict(PartitionKey=QUIESCENCE_PARTITION, RowKey=hashlib.sha256(user.encode()).hexdigest(),
                   libraryId=user, fenceId=uuid.uuid4().hex, operator=operator,
                   reason=reason, proven=False)
        try:
            app.jobs_table_client.create_entity(row)
        except ResourceExistsError:
            pass
        existing = _quiescence_row(user)
    return dict(fenceId=existing['fenceId'], proven=bool(existing.get('proven')))


def operator_prove(user, fence_id, *, guard, operator, reason, all_writers_stopped,
                   no_paused_requests, legacy_writers_stopped):
    import app
    from index_publication import require_catalog_guard
    require_catalog_guard(user, guard)
    guard.check()
    if not (operator and reason and all_writers_stopped is True and
            no_paused_requests is True and legacy_writers_stopped is True):
        raise ValueError('Explicit all-writer quiescence proof required')
    row = _quiescence_row(user)
    if row is None or row.get('fenceId') != fence_id:
        raise SourceBusy('Operator fence changed or missing')
    _check_publication_claims(user)
    guard.check()
    app.jobs_table_client.update_entity(dict(row, proven=True, operator=operator, reason=reason,
        proofId=uuid.uuid4().hex, provenAt=time.time()), **_cas(row))
    return dict(fenceId=fence_id, proven=True)


def quiescence_check(user, *, guard, fence_id=None):
    """Return a rechecked proof callback; caller MUST hold the real catalog lock."""
    from index_publication import require_catalog_guard
    require_catalog_guard(user, guard)
    guard.check()
    row = _quiescence_row(user)
    if (row is None or row.get('libraryId') != user or row.get('proven') is not True
            or not row.get('proofId') or (fence_id is not None and row['fenceId'] != fence_id)):
        raise SourceBusy('Explicit operator all-writer quiescence proof required')
    version = etag(row)
    if not version or version == '*':
        raise RuntimeError('Quiescence proof requires genuine ETags')

    def check():
        guard.check()
        live = _quiescence_row(user)
        if live is None or etag(live) != version or live.get('proofId') != row['proofId']:
            raise SourceBusy('Operator quiescence proof changed')
        _check_publication_claims(user)
        guard.check()
    check()
    return check


def recover_quiesced_source_reservations(user, *, guard, fence_id):
    import app
    check = quiescence_check(user, guard=guard, fence_id=fence_id)
    store = configured_store()
    recovered = []
    orphaned_reservations = {}
    for kind in KINDS:
        check()
        # The durable wakeup ledger precedes coverage reset. Crash at any step
        # leaves the fence + IDs + intent, never a silently clean artifact.
        work = WorkStore(app.jobs_table_client, partition=TAG_PARTITION if kind == 'tag_embedding'
                         else PARTITION)
        work.request(user, SCOPES.get(kind, kind))
        pk = partition(user, kind)
        cleaned = []
        try:
            raw_entities = store.table.query_entities(f"PartitionKey eq '{pk}' and RowKey ne '{CONTROL}'")
            res_entities = [r for r in raw_entities if str(r.get('RowKey', '')).startswith('res-')]
            for res in res_entities:
                cleaned.append({
                    'reservationId': res.get('reservationId'),
                    'owner': res.get('owner'),
                    'operation': res.get('operation'),
                    'startedAt': res.get('startedAt'),
                    'idsCount': res.get('idsCount'),
                })
                try:
                    store.table.delete_entity(pk, res['RowKey'])
                except Exception:
                    pass
        except Exception:
            pass
        if cleaned:
            orphaned_reservations[kind] = cleaned
            LOG.info(f"Recovered orphaned reservations for library={user!r}, kind={kind}: {cleaned}")
        store._change(user, kind, lambda row: row.update(active=0, fullRequired=True,
                                                       base='', coveredRevision=''))
        recovered.append(kind)
    check()
    result = dict(recovered=recovered, fenceId=fence_id, writersFenced=True)
    if orphaned_reservations:
        result['orphanedReservations'] = orphaned_reservations
    return result


def resume_quiesced_sources(user, *, guard, fence_id):
    import app
    check = quiescence_check(user, guard=guard, fence_id=fence_id)
    row = _quiescence_row(user)
    check()
    app.jobs_table_client.delete_entity(row['PartitionKey'], row['RowKey'], etag=etag(row),
                                       match_condition=MatchConditions.IfNotModified)
    guard.check()
    return dict(fenceId=fence_id, writersFenced=False)


class SourceBusy(RuntimeError):
    """An active source writer is not a failed build or a poison job."""


def partition(user, kind):
    if kind not in KINDS:
        raise ValueError('Unknown automatic artifact kind')
    return 'automatic-' + hashlib.sha256(f'{user}\0{kind}'.encode()).hexdigest()


def _cas(row):
    version = etag(row)
    if not version or version == '*':
        raise RuntimeError('Coverage requires genuine ETags')
    return dict(mode=UpdateMode.REPLACE, etag=version,
                match_condition=MatchConditions.IfNotModified)


@dataclass
class Capture:
    control: object
    dirty: DirtySnapshot

    @property
    def ids(self):
        return [row['sourceId'] for row in self.dirty.rows.values()]


class CoverageStore:
    def __init__(self, table):
        if table is None:
            raise RuntimeError('Automatic index coverage storage unavailable')
        self.table = table

    def control(self, user, kind):
        try:
            return self.table.get_entity(partition(user, kind), CONTROL)
        except ResourceNotFoundError:
            return None

    def ensure(self, user, kind):
        row = self.control(user, kind)
        if row is not None:
            if row.get('coverageVersion') != VERSION:
                raise RuntimeError('Unsupported automatic coverage version')
            return row
        try:
            self.table.create_entity(dict(PartitionKey=partition(user, kind), RowKey=CONTROL,
                libraryId=user, kind=kind, coverageVersion=VERSION, revision=uuid.uuid4().hex,
                active=0, fullRequired=True, base='', coveredRevision=''))
        except ResourceExistsError:
            pass
        return self.control(user, kind)

    def _change(self, user, kind, change, ids=(), deleted=False, extra_ops=()):
        # IDs and the revision advance commit in the SAME partition transaction.
        ids = list(dict.fromkeys(str(value) for value in ids if value))
        limit_page_0 = max(1, 99 - len(extra_ops))
        if ids:
            pages = [ids[:limit_page_0]]
            remaining = ids[limit_page_0:]
            if remaining:
                pages.extend([remaining[i:i + 99] for i in range(0, len(remaining), 99)])
        else:
            pages = [[]]
        result = None
        for page_index, page in enumerate(pages):
            for attempt in range(20):
                current = self.ensure(user, kind)
                row = dict(current, revision=uuid.uuid4().hex)
                if page_index == 0:
                    change(row)
                ops = [('update', row, _cas(current))]
                ops.extend(('upsert', dict(PartitionKey=row['PartitionKey'],
                    RowKey='id-' + hashlib.sha256(value.encode()).hexdigest(),
                    sourceId=value, tombstone=bool(deleted), revision=row['revision'])) for value in page)
                if page_index == 0 and extra_ops:
                    ops.extend(extra_ops)
                try:
                    self.table.submit_transaction(ops)
                    result = row
                    break
                except (ResourceModifiedError, ResourceExistsError):
                    if attempt == 19:
                        raise
                except Exception as error:
                    # Azure reports an ETag conflict inside a batch this way.
                    if getattr(error, 'status_code', None) != 412 or attempt == 19:
                        raise
                # Immediate CAS retries repeatedly lose to the same hot
                # coverage control row during upload fanout. Keep the durable
                # reservation and bounded retry policy, but yield long enough
                # for the winning transaction to publish its next ETag.
                time.sleep(random.uniform(0.001, min(0.25, 0.002 * (2 ** attempt))))
        return result

    def begin(self, user, kind, ids, *, uncertain=False, deleted=False,
              reservation_id=None, owner=None, operation=None):
        reservation_id = reservation_id or uuid.uuid4().hex
        res_entity = dict(
            PartitionKey=partition(user, kind),
            RowKey=f'res-{reservation_id}',
            reservationId=reservation_id,
            libraryId=user,
            kind=kind,
            owner=str(owner or os.getenv('CONTAINER_APP_NAME') or os.getenv('HOSTNAME') or 'source_writer'),
            operation=str(operation or 'source_write'),
            startedAt=time.time(),
            idsCount=len(ids),
        )
        def reserve(row):
            row['active'] = int(row.get('active') or 0) + 1
            row['fullRequired'] = bool(row.get('fullRequired') or uncertain)
        # If page 2 fails, the durable active reservation intentionally remains.
        self._change(user, kind, reserve, ids, deleted, extra_ops=[('upsert', res_entity)])
        return reservation_id

    def finish(self, user, kind, reservation_id=None, *, failed=False):
        pk = partition(user, kind)
        if reservation_id is None:
            try:
                raw_rows = self.table.query_entities(f"PartitionKey eq '{pk}' and RowKey ne '{CONTROL}'")
                res_rows = [r for r in raw_rows if str(r.get('RowKey', '')).startswith('res-')]
                if res_rows:
                    reservation_id = res_rows[0].get('reservationId')
            except Exception:
                pass

        extra_ops = []
        if reservation_id:
            res_key = f'res-{reservation_id}'
            extra_ops = [('delete', dict(PartitionKey=pk, RowKey=res_key))]

        def complete(row):
            if int(row.get('active') or 0) <= 0:
                raise RuntimeError('Unbalanced source reservation')
            row['active'] -= 1
            row['fullRequired'] = bool(row.get('fullRequired') or failed)

        try:
            self._change(user, kind, complete, extra_ops=extra_ops)
        except Exception as error:
            is_not_found = isinstance(error, ResourceNotFoundError) or getattr(error, 'status_code', None) == 404
            if is_not_found and extra_ops:
                self._change(user, kind, complete)
            else:
                raise

    def reservations(self, user, kind):
        pk = partition(user, kind)
        try:
            raw_rows = self.table.query_entities(
                f"PartitionKey eq '{pk}' and RowKey ne '{CONTROL}'")
            return [dict(r) for r in raw_rows if str(r.get('RowKey', '')).startswith('res-')]
        except Exception:
            return []

    def capture(self, user, kind):
        row = self.ensure(user, kind)
        if row.get('active'):
            raise SourceBusy(f'Active {kind} source intent for {user}')
        pk = row['PartitionKey']
        raw_rows = self.table.query_entities(
            f"PartitionKey eq '{pk}' and RowKey ne '{CONTROL}'")
        dirty = DirtySnapshot((r for r in raw_rows if not str(r.get('RowKey', '')).startswith('res-')), max_rows=LIMIT)
        return Capture(row, dirty)

    def check(self, user, kind, captured):
        current = self.control(user, kind)
        if (current is None or current.get('active') or
                current.get('revision') != captured.control['revision'] or
                etag(current) != etag(captured.control)):
            raise SourceBusy('Source changed during artifact build')

    def certify(self, user, kind, captured, base, *, full):
        import index_publication
        index_publication.check()
        if not base:
            raise RuntimeError('Artifact publication has no durable base')
        if captured.dirty.truncated and not full:
            raise RuntimeError('Truncated captures cannot certify an incremental build')
        self.check(user, kind, captured)
        current = captured.control
        row = dict(current, base=base, coveredRevision=current['revision'],
                   fullRequired=False)
        # Certification is after the actual pointer commit, before any dirty ack.
        try:
            self.table.update_entity(row, **_cas(current))
        except ResourceModifiedError as error:
            raise SourceBusy('Source changed before coverage certification') from error
        index_publication.check()
        if not captured.dirty.truncated:
            acknowledge_dirty(self.table, captured.dirty)
        else:
            # A full scan covers the entire idle revision, including IDs not in
            # the bounded lookahead. Drain in bounded ETag batches. A new writer
            # gets a DIFFERENT revision; never acknowledge its rows.
            while True:
                raw_rows = self.table.query_entities(
                    f"PartitionKey eq '{row['PartitionKey']}' and RowKey ne '{CONTROL}'")
                snapshot = DirtySnapshot((r for r in raw_rows if not str(r.get('RowKey', '')).startswith('res-')), max_rows=LIMIT)
                # Filter against source revision via the control ETag first;
                # if any writer started, leave all remaining rows for next job.
                live = self.control(user, kind)
                if live['revision'] != current['revision'] or live.get('active'):
                    break
                index_publication.check()
                acknowledge_dirty(self.table, snapshot)
                if not snapshot.truncated:
                    break


def configured_store():
    import storage_utils as su
    return CoverageStore(su._CTX.get('search_index_dirty_table_client'))


def wave_pending(user):
    """Guarded waves keep their receipts/aliases; seed coverage after completion."""
    import storage_utils as su
    if not su._CTX.get('wave_owned') or not su._CTX['wave_owned'](user):
        return False
    import app
    from upload_waves import ShadowWaves
    if app.jobs_table_client is None:
        return True
    waves = ShadowWaves(app.jobs_table_client, user)
    control = waves._get('control')
    tail = waves._get(waves.wave_key(control['tailWaveId'])) if control and control.get('tailWaveId') else None
    return bool(tail and tail['state'] not in ('complete', 'cancelled'))


def people_completion(user, ids):
    """Post-FAISS replay hook; pre-write ownership coverage is in SourceTable."""
    import storage_utils as su
    from wave_policy import source_change_active
    if not su.DURABLE_INDEX_UPDATES or not ids:
        return
    if source_change_active() and su._CTX.get('wave_owned') and su._CTX['wave_owned'](user):
        return
    configured_store()._change(user, 'people_embedding', lambda row: None, ids)
    schedule(user, 'people_embedding')


def schedule(user, kind):
    import storage_utils as su
    hook = su.METADATA_INDEX_REQUEST_HOOK
    if hook is None or hook(user, SCOPES.get(kind, kind)) == 'unavailable':
        raise RuntimeError('Automatic artifact scheduling unavailable')


def _is_internal_source_row(source, user, key):
    """Exact reserved identities, never filename/field-based media admission.

    Metadata reserves clustering_maintenance for per-library coordination and
    __system__ for media_delegation_key_YYYYMMDD and ipwork_sweep_lock records.
    Legacy jobs and performance partitions hold job status and throughput
    records (still read by app fallback paths). All four whole partitions are
    internal. No library-partition sentinel is
    reserved here: __init__ belongs to the separate trash index table. Preserve
    media reservations, processing rows, tombstones and odd/extensionless names,
    even when finalization fields are absent. Hash control identities are shared
    with their schema. Apply this before old reads AND source admission checks.
    """
    if source == 'metadata':
        return user in {'clustering_maintenance', '__system__', 'jobs', 'performance'}
    if source == 'hash':
        from upload_hash_snapshot import CONTROL_ROWS
        return key in CONTROL_ROWS
    return False


def mutation_targets(source, table, operation, entity, options):
    """Exclude exact internal rows, then derive old AND new owners before writes."""
    import storage_utils as su
    user, key = str(entity.get('PartitionKey') or ''), str(entity.get('RowKey') or '')
    if not user or not key:
        raise ValueError('Source mutation requires partition and row identity')
    if _is_internal_source_row(source, user, key):
        return user, {}, False, operation == 'delete'
    old, uncertain = None, False
    try:
        old = table.get_entity(user, key)
    except ResourceNotFoundError:
        pass
    except Exception:
        uncertain = True
    deleted = operation == 'delete'
    conditional = bool(old is not None and operation in ('update', 'delete') and
        options.get('match_condition') == MatchConditions.IfNotModified and
        (options.get('etag') or etag(entity)) == etag(old))
    if source == 'metadata':
        # A field diff is safe only against the very same source ETag. Partial
        # unconditional merges use ALL supplied fields, not a racy prior read.
        fields = {k for k in entity if k not in {'PartitionKey', 'RowKey'} and
                  (not conditional or entity.get(k) != old.get(k))}
        if operation == 'update' and options.get('mode') == UpdateMode.REPLACE:
            fields.update(set(old or {}) - set(entity))
        if operation == 'upsert' and options.get('mode') == UpdateMode.REPLACE:
            # Replacement may remove fields from a concurrently changed row;
            # a pre-read without source CAS cannot enumerate that difference.
            uncertain = True
        fields = {k for k in fields if not k.startswith(('indexPending', 'processingCompletion',
                   'processingLease', 'processingReceipt')) and k not in {'last_processing_update'}}
        kinds = set(su.metadata_index_kinds(fields)).intersection({'vector', 'tag_embedding'})
        if fields - {'photoEmbedding', 'semanticEmbedding'}:
            kinds.add('lexical_artifact')
        if deleted or uncertain:
            kinds.update(('vector', 'tag_embedding', 'lexical_artifact'))
        return user, {kind: [key] for kind in kinds}, uncertain, deleted
    if source == 'embeddings':
        return user, {'vector': [key]}, uncertain, deleted
    if source == 'hash':
        return user, {'hash': [key]}, uncertain, deleted
    if source == 'person':
        return user, {'people_embedding': [key]}, uncertain, deleted
    if source in ('face', 'face_embeddings'):
        face = old
        if source == 'face_embeddings':
            try:
                face = su._CTX['face_table_client'].get_entity(user, key)
            except ResourceNotFoundError:
                face = None
            except Exception:
                uncertain = True
        owners = {str((face or {}).get('personId') or '')}
        if source == 'face' and not deleted:
            owners.add(str(entity.get('personId') or ''))
        owners.discard('')
        # Unconditional face replacement/delete can race an owner change;
        # tracking the observed owners alone is not complete coverage.
        if source == 'face' and old is not None and not conditional:
            uncertain = True
        return user, {'people_embedding': sorted(owners)} if owners or uncertain else {}, uncertain, deleted
    raise ValueError('Unknown source table')


class SourceTable:
    """Reserve before source writes; finalize every successfully begun intent.

    Finish ALL reservations before attempting ALL post-write schedules, even
    when either pass fails. Report failures as an exception group (chained under
    the original source/begin error when present), so successful source writes
    with failed finalization remain caller-visible/retryable. A failed begin is
    NOT balanced: partial/unknown commits intentionally leave active fail-closed.
    Exact internal identities bypass tracking; media admission does not depend
    on filename extensions, processing state or finalization fields.
    """
    MUTATIONS = {'create_entity', 'update_entity', 'upsert_entity', 'delete_entity', 'submit_transaction'}

    def __init__(self, table, source):
        self.table, self.source = table, source

    def __getattr__(self, name):
        method = getattr(self.table, name)
        if name not in self.MUTATIONS:
            return method

        @wraps(method)
        def write(*args, **kwargs):
            import storage_utils as su
            if not su.DURABLE_INDEX_UPDATES:
                return method(*args, **kwargs)
            if name == 'submit_transaction':
                operations = list(args[0] if args else kwargs['entity_operations'])
                if args:
                    args = (operations,) + args[1:]
                else:
                    kwargs['entity_operations'] = operations
            elif name == 'delete_entity':
                entity = kwargs.get('entity')
                if entity is None and args and isinstance(args[0], dict):
                    entity = args[0]
                if entity is None:
                    entity = dict(PartitionKey=kwargs.get('partition_key', args[0] if args else ''),
                                  RowKey=kwargs.get('row_key', args[1] if len(args) > 1 else ''))
                operations = [('delete', entity, kwargs)]
            else:
                entity = args[0] if args else kwargs['entity']
                operations = [(name.split('_')[0], entity, kwargs)]
            targets, uncertain, deleted = {}, {}, {}
            from wave_policy import source_change_active
            for operation in operations:
                op, entity = operation[:2]
                options = operation[2] if len(operation) > 2 else {}
                user = str(entity.get('PartitionKey') or '')
                key = str(entity.get('RowKey') or '')
                if user and key and _is_internal_source_row(self.source, user, key):
                    continue
                cleanup_check = current_cleanup_check(user)
                if cleanup_check is not None:
                    cleanup_check()
                else:
                    check_source_admission(user)
                user, kinds, unsafe, gone = mutation_targets(self.source, self.table, op, entity, options)
                wave = bool(source_change_active() and su._CTX.get('wave_owned') and su._CTX['wave_owned'](user))
                for kind, ids in kinds.items():
                    pair = (user, kind)
                    targets.setdefault(pair, set()).update(() if wave else ids)
                    uncertain[pair] = uncertain.get(pair, False) or unsafe or wave
                    deleted[pair] = deleted.get(pair, False) or gone
            if not targets:
                return method(*args, **kwargs)
            cleanup = _QUIESCED_CLEANUP.get()
            if cleanup is not None:
                if any(user != cleanup[0] for user, kind in targets):
                    raise RuntimeError('Cross-library quiesced source cleanup')
                cleanup[1]()
                result = method(*args, **kwargs)
                cleanup[1]()
                return result
            store = configured_store()
            reserved = []
            success = False
            source_error = None
            try:
                for (user, kind), ids in sorted(targets.items()):
                    check_source_admission(user)
                    # WorkStore persistence precedes source storage; a queue
                    # failure cannot erase evidence, even if source write fails.
                    if not (source_change_active() and su._CTX.get('wave_owned') and su._CTX['wave_owned'](user)):
                        schedule(user, kind)
                    res_id = store.begin(user, kind, ids, uncertain=uncertain[user, kind],
                                         deleted=deleted[user, kind],
                                         owner=os.getenv('CONTAINER_APP_NAME') or os.getenv('HOSTNAME') or 'source_writer',
                                         operation=f'{self.source}.{name}')
                    reserved.append((user, kind, res_id))
                for user, kind, _ in reserved:
                    check_source_admission(user)
                result = method(*args, **kwargs)
                success = True
                return result
            except BaseException as error:
                source_error = error
                raise
            finally:
                failures = []
                for user, kind, res_id in reserved:
                    try:
                        store.finish(user, kind, reservation_id=res_id, failed=not success)
                    except BaseException as error:
                        error.add_note(f'Automatic source finish failed: library={user!r}, kind={kind}, reservation={res_id}')
                        failures.append(error)
                for user, kind, _ in reserved:
                    try:
                        if not (source_change_active() and su._CTX.get('wave_owned') and su._CTX['wave_owned'](user)):
                            # A very fast worker may consume the pre-write wakeup
                            # before begin(). Advance again AFTER the source commit.
                            schedule(user, kind)
                    except BaseException as error:
                        error.add_note(f'Automatic source schedule failed: library={user!r}, kind={kind}')
                        failures.append(error)
                if failures:
                    finalization = BaseExceptionGroup('Automatic source finalization failed; retry required', failures)
                    if source_error is not None:
                        # Preserve type, identity and traceback of the original
                        # source failure while exposing EVERY finalization error.
                        raise source_error from finalization
                    raise finalization
        return write


def artifact_base(user, kind):
    import storage_utils as su
    if kind == 'hash':
        import upload_hash_snapshot as hashes
        return str(hashes._row(su._CTX['hash_index_table_client'], user, hashes.SNAPSHOT_ROW).get('blobName') or '')
    loader = getattr(su, '_load_' + ('lexical' if kind == 'lexical_artifact' else kind) + '_index_manifest')
    manifest = loader(user)
    if kind == 'lexical_artifact':
        from lexical_incremental import effective_snapshot_version
        return effective_snapshot_version(manifest)
    return str(manifest.get('baseSourceVersion') if manifest.get('dirty') else manifest.get('sourceVersion') or '')


@serialize_catalog
def run(user, kind, *, people_model):
    """Runtime policy: strict targeted API when certified, otherwise full seed."""
    import storage_utils as su
    import vision_utils
    check_source_admission(user)
    if wave_pending(user):
        raise SourceBusy('Guarded wave must finish before automatic artifact seed')
    store = configured_store()
    captured = store.capture(user, kind)
    previous = artifact_base(user, kind)
    control = captured.control
    full = bool(control.get('fullRequired') or not previous or previous != control.get('base')
                or captured.dirty.truncated)
    if not full and not captured.ids and control.get('coveredRevision') == control['revision']:
        return {'status': 'noop'}
    check = lambda: store.check(user, kind, captured)
    check()
    if kind == 'hash':
        import upload_hash_snapshot as hashes
        epoch = hashes._epoch(su._CTX['hash_index_table_client'], user)
        if full:
            receipt = hashes.rebuild_snapshot(user, publication_check=check)
        else:
            receipt = hashes.upgrade_snapshot(user, captured.ids, expected_blob_name=previous,
                complete_dirty_hashes=True, expected_delete_epoch=epoch, publication_check=check,
                strict_source_reads=True)
    else:
        refresh = getattr(su, 'refresh_user_' + ('lexical' if kind == 'lexical_artifact' else kind) + '_index')
        options = {}
        if kind == 'tag_embedding':
            options['model_version'] = vision_utils.get_text_embedding_version()
            if full:
                options['bootstrap_incremental'] = True
        if kind == 'people_embedding':
            if not people_model:
                raise RuntimeError('Configured representative embedding identity required')
            options['model_version'] = people_model
        if not full:
            options.update(previous_source_version=previous, incremental_only=True)
            options['affected_person_ids' if kind == 'people_embedding' else 'affected_filenames'] = captured.ids
        elif kind in ('vector', 'lexical_artifact'):
            options['force_full'] = True
        receipt = refresh(user, **options)
        # Strict APIs return None on incompatibility OR IO/publication failure.
        # Do NOT hide a failed commit behind a second build in the same attempt.
        # Persist full fallback for the next durable retry instead.
        if receipt is None:
            store._change(user, kind, lambda row: row.update(fullRequired=True))
            raise RuntimeError('Artifact deferred; retained dirty IDs for full retry')
    if receipt is None:
        raise RuntimeError('Artifact did not publish')
    check()
    base = artifact_base(user, kind)
    store.certify(user, kind, captured, base, full=full)
    result = {'status': 'full' if full else 'incremental', 'kind': kind, 'sourceVersion': base,
              'dirtyCount': len(captured.ids), 'truncated': captured.dirty.truncated}
    if isinstance(receipt, dict):
        result['rowCount'] = receipt.get('rows', 0)
    elif hasattr(receipt, 'row_count'):
        result['rowCount'] = receipt.row_count
    elif hasattr(receipt, 'row_keys'):
        result['rowCount'] = len(receipt.row_keys)
    elif hasattr(receipt, 'person_ids'):
        result['rowCount'] = len(receipt.person_ids)
    return result
