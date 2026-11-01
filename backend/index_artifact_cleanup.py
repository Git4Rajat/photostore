"""Bounded, worker-only GC for immutable per-library index generations.

No read-side hooks. ``cleanup_user_index_artifacts`` requires a serialization
factory yielding a live ``check()`` guard. ALL catalog publishers (including
model-tag and hash), not just wave builders, must share that library lock from
base capture through pointer commit. A wave-only/no-op lock is NOT sufficient.
Without this integration the safe result is deferred, never best-effort GC.

Immutable targets are create-only, newly generated UUID names and never rebound
to old generations. Source coverage reservations fence paused builders/writers;
publication leases and the shared catalog lock fence pointer replacement. Blob
ETags fence deletion of changed bytes, NOT replacement of a manifest pointer.
Grace applies to BOTH bytes and the last pointer update (retirement time): an
old blob freshly retired may still have readers. Readers must finish within
the configured grace; an indefinitely paused reader needs its own reservation.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import json
import math
import os
import re

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError

import index_publication

KINDS = ('vector', 'people_embedding', 'tag_embedding', 'lexical_artifact', 'hash')
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REFERENCES = 128


class CleanupDeferred(RuntimeError):
    """Missing/corrupt evidence, incomplete survey or concurrent publication."""


@dataclass(frozen=True)
class Policy:
    grace_seconds: float = 24 * 60 * 60
    retain_generations: int = 2
    max_deletions: int = 100
    page_size: int = 256
    max_pages: int = 100

    def __post_init__(self):
        if (not isinstance(self.grace_seconds, (int, float))
                or isinstance(self.grace_seconds, bool)
                or not math.isfinite(self.grace_seconds) or self.grace_seconds <= 0):
            raise ValueError('Cleanup grace must be finite and positive')
        for name, minimum, maximum in [('retain_generations', 2, 128),
                                      ('max_deletions', 0, 10000),
                                      ('page_size', 1, 1000), ('max_pages', 1, 1000)]:
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError('Invalid cleanup ' + name)

    @classmethod
    def configured(cls):
        return cls(grace_seconds=float(os.getenv('INDEX_ARTIFACT_CLEANUP_GRACE_SECONDS', '86400')),
                   retain_generations=int(os.getenv('INDEX_ARTIFACT_CLEANUP_RETAIN_GENERATIONS', '2')),
                   max_deletions=int(os.getenv('INDEX_ARTIFACT_CLEANUP_MAX_DELETIONS', '100')))


@dataclass(frozen=True)
class ArtifactScope:
    kind: str
    container: str
    prefix: str
    suffixes: tuple
    manifest: str | None
    schema: object

    def generation(self, name):
        if not isinstance(name, str):
            return None
        for suffix in self.suffixes:
            match = re.fullmatch(re.escape(self.prefix) + r'([0-9a-f]{32})' + re.escape(suffix), name)
            if match:
                return match[1]
        return None


def scopes(storage, user):
    if not isinstance(user, str) or not user or user != user.strip():
        raise ValueError('A nonempty exact library ID is required')
    key = storage._vector_index_blob_key(user)
    result = []
    for kind, suffixes, schema in [
            ('vector', ('.npz',), 1),
            ('people_embedding', ('.npz',), '1'),
            ('tag_embedding', ('.npz', '.sqlite'), storage._TAG_EMBEDDING_INDEX_SCHEMA_VERSION)]:
        result.append(ArtifactScope(kind, getattr(storage, '_' + kind + '_index_container_name')(),
                                    key + '.', suffixes,
                                    getattr(storage, '_' + kind + '_index_manifest_blob_name')(user), schema))
    result.append(ArtifactScope('lexical_artifact', storage._lexical_index_container_name(),
                                key + '.', ('.json.gz',),
                                storage._lexical_index_manifest_blob_name(user),
                                storage._LEXICAL_INDEX_SCHEMA_VERSION))
    result.append(ArtifactScope('hash', storage._lexical_index_container_name(),
                                key + '-upload-hashes-', ('.json.gz',), None, 1))
    return tuple(result)


def _version(value):
    if isinstance(value, dict):
        result = value.get('etag')
    else:
        result = getattr(value, 'etag', None)
    if not result or result == '*':
        raise CleanupDeferred('Evidence lacks a genuine ETag')
    return str(result)


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CleanupDeferred('Evidence lacks an aware storage timestamp')
    return value.timestamp()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CleanupDeferred('Duplicate manifest field')
        result[key] = value
    return result


def _references(scope, row, user):
    # People full manifests historically have embeddingVersion, not schemaVersion.
    schema = row.get('schemaVersion', row.get('embeddingVersion')
                     if scope.kind == 'people_embedding' else None)
    if (type(schema) is not type(scope.schema) or schema != scope.schema
            or row.get('userId', row.get('PartitionKey')) != user):
        raise CleanupDeferred('Unknown manifest schema or library')
    fields = {'blobName', 'npzBlobName', 'stateBlobName', 'contributionBlobName',
              'baseBlobName', 'previousBlobName', 'snapshotBlobName'}
    refs = set()

    def collect(value, depth=0):
        if depth > 4:
            raise CleanupDeferred('Unrecognized snapshot lineage')
        if isinstance(value, str):
            # Fixed aliases can be live references, but are never candidates.
            aliases = (set() if scope.kind == 'hash' else
                       {scope.prefix + suffix.lstrip('.') for suffix in scope.suffixes})
            if scope.generation(value) is None and value not in aliases:
                raise CleanupDeferred('Invalid or cross-library artifact pointer')
            refs.add(value)
        elif isinstance(value, list):
            if len(value) > MAX_REFERENCES:
                raise CleanupDeferred('Oversized snapshot lineage')
            for item in value:
                collect(item, depth + 1)
        elif isinstance(value, dict):
            if not value or set(value) - fields:
                raise CleanupDeferred('Unrecognized snapshot lineage fields')
            for item in value.values():
                collect(item, depth + 1)
        else:
            raise CleanupDeferred('Invalid artifact pointer')
        if len(refs) > MAX_REFERENCES:
            raise CleanupDeferred('Too many manifest references')

    for field, value in row.items():
        if field in fields or field == 'snapshotLineage':
            collect(value)
        elif field.lower().endswith(('blobname', 'blobnames')):
            raise CleanupDeferred('Unknown manifest pointer field')
    if scope.kind == 'hash' and not row.get('blobName'):
        raise CleanupDeferred('Missing hash snapshot pointer')
    # A sidecar is part of its generation even when one alias is absent from a
    # legacy manifest. Never collect half of a live multi-blob tag publication.
    generations = {scope.generation(name) for name in refs} - {None}
    refs.update(scope.prefix + generation + suffix
                for generation in generations for suffix in scope.suffixes)
    return refs


def _reference_snapshot(storage, service, scope, user, expected_base):
    if scope.kind == 'hash':
        from upload_hash_snapshot import SNAPSHOT_ROW
        from index_work import etag
        table = storage._CTX.get('hash_index_table_client')
        if table is None:
            raise CleanupDeferred('Hash snapshot table unavailable')
        row = table.get_entity(user, SNAPSHOT_ROW)
        version = _version({'etag': etag(row)})
        modified = row.get('Timestamp')
    else:
        blob = service.get_blob_client(container=scope.container, blob=scope.manifest)
        props = blob.get_blob_properties()
        version, modified = _version(props), props.last_modified
        raw = blob.download_blob(offset=0, length=MAX_MANIFEST_BYTES + 1,
                                 etag=version, match_condition=MatchConditions.IfNotModified).readall()
        if len(raw) > MAX_MANIFEST_BYTES:
            raise CleanupDeferred('Oversized manifest')
        row = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(row, dict):
            raise CleanupDeferred('Invalid manifest')
    if scope.kind == 'hash':
        base = row.get('blobName')
    elif scope.kind == 'lexical_artifact':
        from lexical_incremental import effective_snapshot_version
        base = effective_snapshot_version(row)
    else:
        base = row.get('baseSourceVersion') if row.get('dirty') else row.get('sourceVersion')
    if not base or base != expected_base:
        # A pointer commit without ledger certification is NOT a new GC base.
        # Preserve the old certified generation until a worker repairs coverage.
        raise CleanupDeferred('Manifest does not match certified snapshot lineage')
    return version, _time(modified), _references(scope, row, user)


def _idle_snapshot(storage, jobs, user, now, kind):
    """Certify only this output's source; keep a global active-claim gate.

    SCOPE_KINDS describes projections, not every builder's artifact inputs.
    Full/reconcile/compact and shared lexical builders can consume multiple
    outputs, so their work ETags remain relevant to every family. Unmapped
    active claims conservatively block all GC; an unused family's absent or
    stale source control does not. The universal catalog lock is unchanged.
    """
    from automatic_index_updates import CoverageStore, VERSION, TAG_PARTITION, CONTROL, partition as source_partition
    from index_work import WorkStore, PARTITION, etag
    from index_updates import SCOPE_KINDS
    store = CoverageStore(storage._CTX.get('search_index_dirty_table_client'))
    versions = []
    row = store.control(user, kind)
    if (row is None or type(row.get('coverageVersion')) is not int
        or row.get('coverageVersion') != VERSION
            or row.get('libraryId') != user or row.get('kind') != kind
            or row.get('PartitionKey') != source_partition(user, kind) or row.get('RowKey') != CONTROL
            or type(row.get('active')) is not int or row['active'] != 0
            or row.get('fullRequired') is not False
            or not row.get('revision') or row.get('coveredRevision') != row['revision']
            or not row.get('base')):
        raise CleanupDeferred('SourceBusy or uncertified full source coverage: ' + kind)
    versions.append((kind, _version({'etag': etag(row)}), row['base']))
    if jobs is None:
        raise CleanupDeferred('Publication work storage unavailable')
    related = {'full', 'reconcile', 'compact', 'search', 'light', 'people',
               'albums', 'sort', 'access', 'lexical_artifact', kind}
    if kind == 'hash':
        related.update(('dedup', 'dedup-idle'))
    for partition in (PARTITION, TAG_PARTITION):
        for scope in SCOPE_KINDS:
            try:
                row = jobs.get_entity(partition, WorkStore.key(user, scope))
            except ResourceNotFoundError:
                continue
            if row.get('libraryId') != user or row.get('scope') != scope:
                raise CleanupDeferred('Invalid publication work identity')
            until = float(row.get('leaseUntil') or 0)
            if not math.isfinite(until) or until > now:
                raise CleanupDeferred('Active publication lease')
            if scope in related:
                versions.append((partition, scope, _version({'etag': etag(row)})))
    return tuple(versions)


def _items(service, scope, policy, check, *, bounded=True):
    # Azure ItemPaged pages are bounded by results_per_page. No list(container)
    # and no query without the exact SHA-256 library prefix, even during purge.
    pages = service.get_container_client(scope.container).list_blobs(
        name_starts_with=scope.prefix, results_per_page=policy.page_size).by_page()
    for page_number, page in enumerate(pages):
        if bounded and page_number >= policy.max_pages:
            raise CleanupDeferred('Candidate survey page cap reached')
        check()
        for item in page:
            check()
            if scope.generation(item.name) is not None:
                yield item


def _young_sibling(service, scope, item, now, policy, check):
    """Protect the whole tag generation if either existing sidecar is young.

    Fixed-size point reads avoid retaining an unbounded set of young UUIDs.
    Absent siblings are allowed for old, never-committed orphan generations;
    other read failures defer the family rather than authorizing half-GC.
    """
    generation = scope.generation(item.name)
    for suffix in scope.suffixes:
        name = scope.prefix + generation + suffix
        if name == item.name:
            continue
        check()
        client = service.get_blob_client(container=scope.container, blob=name)
        try:
            props = client.get_blob_properties()
        except ResourceNotFoundError:
            continue
        if now - _time(props.last_modified) < policy.grace_seconds:
            return True
    return False


@contextmanager
def artifact_catalog_serialization(user, *, storage=None):
    """Universal catalog lock for the publisher/cleanup integration handoff.

    Unlike index_publication.catalog_serialization, this does NOT no-op for
    non-wave libraries. Use for ALL artifact publishers, from capture through
    commit, as well as GC. Do not nest inside an already held catalog lock.
    Merely using it for GC, without migrating publishers, is insufficient.
    """
    if storage is None:
        import storage_utils as storage
    scopes(storage, user)
    service = storage._CTX.get('blob_service_client')
    if service is None:
        raise CleanupDeferred('Catalog serialization storage unavailable')
    with index_publication.catalog_serialization(None, user, service,
            storage._lexical_index_container_name(), require_real=True) as guard:
        guard.check()
        yield guard
        guard.check()


def cleanup_user_index_artifacts(user, *, storage=None, jobs=None, serialization=None,
                                 policy=None, now=None, kinds=KINDS):
    """Return aggregate and per-kind retryable reports with exact deleted counts.

    Each family completes its entire bounded retention survey before deleting
    any of its bytes. Failed evidence defers only that family; source and
    pointer CAS proofs are rechecked per deletion under the SAME renewable
    library catalog lock. RAM is bounded by one family's references/retention.
    The deletion budget is global and never exceeds 100 blobs per call.

    ``serialization(user)`` must hold the universal catalog lock, yield a guard
    with check(), and cover every publisher/base reader for this library. Call
    AFTER completing/releasing maintenance work, never within its active claim.
    Ordinary HTTP handlers must not invoke this entry point.
    """
    # Do not accept core's inline-publisher or vision's model-owned exceptions.
    if os.getenv('APP_ROLE', 'core').strip().lower() != 'index-worker':
        from index_ownership import IndexOwnershipError
        raise IndexOwnershipError('Artifact cleanup requires APP_ROLE=index-worker')
    if storage is None:
        import storage_utils as storage
    policy = policy or Policy.configured()
    now = _time(now or datetime.now(timezone.utc))
    selected = tuple(kinds)
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(KINDS):
        raise ValueError('Unknown or duplicate cleanup kind')
    all_scopes = scopes(storage, user)
    reports = {kind: dict(status='deferred', deleted=0, examined=0,
                          reason='catalog serialization required') for kind in selected}
    report = dict(status='deferred', deleted=0, examined=0,
                  reason='catalog serialization required', kinds=reports)

    def finish():
        report['deleted'] = sum(row['deleted'] for row in reports.values())
        report['examined'] = sum(row['examined'] for row in reports.values())
        statuses = {row['status'] for row in reports.values()}
        if statuses == {'complete'}:
            report['status'] = 'complete'
        elif 'capped' in statuses:
            report['status'] = 'capped'
        elif 'complete' in statuses or report['deleted']:
            report['status'] = 'partial'
        else:
            report['status'] = 'deferred'
        report['reason'] = '; '.join(kind + ': ' + row['reason']
                                    for kind, row in reports.items() if row['reason'])
        return report

    if serialization is None or policy.max_deletions == 0:
        if policy.max_deletions == 0:
            for row in reports.values():
                row.update(status='capped', reason='deletion cap reached')
        return finish()
    service = storage._CTX.get('blob_service_client')
    limit = min(100, policy.max_deletions)
    deleted = 0
    try:
        with serialization(user) as guard:
            def check():
                guard.check()
                index_publication.check()
                index_publication.catalog_check()

            check()
            for scope in all_scopes:
                if scope.kind not in selected:
                    continue
                result = reports[scope.kind]
                try:
                    check()
                    idle = _idle_snapshot(storage, jobs, user, now, scope.kind)
                    base = idle[0][2]
                    version, modified, refs = _reference_snapshot(storage, service, scope, user, base)
                    if now - modified < policy.grace_seconds:
                        raise CleanupDeferred('Pointer retirement is inside reader grace')
                    newest = []
                    for item in _items(service, scope, policy, check):
                        result['examined'] += 1
                        generation = scope.generation(item.name)
                        if item.name in refs:
                            continue
                        entry = (_time(item.last_modified), generation)
                        existing = next((i for i, value in enumerate(newest)
                                         if value[1] == generation), None)
                        if existing is not None:
                            # A newer sidecar retires the entire generation,
                            # not just the first suffix encountered in listing.
                            if entry > newest[existing]:
                                newest[existing] = entry
                                heapq.heapify(newest)
                            continue
                        heapq.heappush(newest, entry)
                        if len(newest) > policy.retain_generations - 1:
                            heapq.heappop(newest)
                    retained = {generation for _, generation in newest}
                    for item in _items(service, scope, policy, check):
                        if (item.name in refs or scope.generation(item.name) in retained
                                or now - _time(item.last_modified) < policy.grace_seconds):
                            continue
                        if _young_sibling(service, scope, item, now, policy, check):
                            continue
                        if deleted >= limit:
                            result.update(status='capped', reason='deletion cap reached')
                            break
                        candidate_version = _version(item)
                        client = service.get_blob_client(container=scope.container, blob=item.name)
                        props = client.get_blob_properties()
                        if (_version(props) != candidate_version
                                or now - _time(props.last_modified) < policy.grace_seconds):
                            raise CleanupDeferred('Candidate changed or became young')
                        check()
                        if _idle_snapshot(storage, jobs, user, now, scope.kind) != idle:
                            raise CleanupDeferred('Source coverage changed')
                        live = _reference_snapshot(storage, service, scope, user, base)
                        if live != (version, modified, refs):
                            raise CleanupDeferred('Manifest changed during cleanup')
                        check()
                        try:
                            client.delete_blob(etag=candidate_version,
                                               match_condition=MatchConditions.IfNotModified)
                        except ResourceNotFoundError:
                            continue
                        result['deleted'] += 1
                        deleted += 1
                    else:
                        result.update(status='complete', reason='')
                    check()
                except Exception as error:
                    result.update(status='partial' if result['deleted'] else 'deferred',
                                  reason=f'{type(error).__name__}: {error}')
                    # A family failure can be isolated only while the shared
                    # renewable guard remains live. Never continue after loss.
                    check()
            check()
    except Exception as error:
        if all(row['status'] == 'complete' for row in reports.values()):
            # A context-exit renewal/check failure is still a retryable pass,
            # even if all deletions completed before the guard failed.
            reports[scope.kind]['status'] = 'partial' if reports[scope.kind]['deleted'] else 'deferred'
        for row in reports.values():
            if row['status'] != 'complete':
                row.update(status='partial' if row['deleted'] else 'deferred',
                           reason=f'{type(error).__name__}: {error}')
    return finish()


def iter_immutable_locations(storage, user, *, kinds=KINDS):
    """Scoped streaming purge discovery; no legacy aliases/wave projections."""
    service = storage._CTX.get('blob_service_client')
    if service is None:
        return
    for scope in scopes(storage, user):
        if scope.kind in kinds:
            for item in _items(service, scope, Policy(), lambda: None, bounded=False):
                yield scope.container, item


def purge_immutable_artifacts(storage, user, *, kinds=KINDS, check=lambda: None):
    """Existing destructive-purge caller owns quiescence; never used as GC.

    Stream all exact immutable generations, including uncommitted sidecars.
    ETag conflicts/errors propagate so library purge is retryable.
    """
    deleted = 0
    for container, item in iter_immutable_locations(storage, user, kinds=kinds):
        check()
        client = storage._CTX['blob_service_client'].get_blob_client(container=container, blob=item.name)
        try:
            client.delete_blob(etag=_version(item), match_condition=MatchConditions.IfNotModified)
            deleted += 1
        except ResourceNotFoundError:
            pass
    return deleted


def purge_automatic_state(storage, jobs, user, *, quiesced_check):
    """Quiesced account/library purge ONLY; includes dirty IDs and model jobs.

    The callback must assert the durable account/library fence AND drained
    in-flight source/publisher workers on every deletion. Active orphan coverage
    may then be purged; a source reservation timeout alone is not quiescence.
    Never attach this to a delete-photo flow or ordinary maintenance.
    """
    from automatic_index_updates import KINDS, TAG_PARTITION, partition
    from index_work import PARTITION, etag
    table = storage._CTX.get('search_index_dirty_table_client')
    if table is None or jobs is None or not callable(quiesced_check):
        raise CleanupDeferred('Quiesced purge storage/check unavailable')
    scopes(storage, user)  # Validate the exact identity before querying.
    count = 0

    def delete(client, row):
        nonlocal count
        quiesced_check()
        try:
            client.delete_entity(row['PartitionKey'], row['RowKey'], etag=_version({'etag': etag(row)}),
                                 match_condition=MatchConditions.IfNotModified)
            count += 1
        except ResourceNotFoundError:
            pass

    quiesced_check()
    for kind in KINDS:
        pk = partition(user, kind)
        for row in table.query_entities(f"PartitionKey eq '{pk}'", results_per_page=256):
            if row['PartitionKey'] != pk:
                raise CleanupDeferred('Cross-library purge row')
            delete(table, row)
    escaped = user.replace("'", "''")
    for pk in (PARTITION, TAG_PARTITION):
        for row in jobs.query_entities(
                f"PartitionKey eq '{pk}' and libraryId eq '{escaped}'", results_per_page=256):
            if row['PartitionKey'] != pk or row.get('libraryId') != user:
                raise CleanupDeferred('Cross-library purge job')
            delete(jobs, row)
    return count


def purge_quiesced_library_artifacts(storage, jobs, user, *, quiesced_check):
    """Purge-route handoff, inside its durable fence and after workers drain.

    Keep existing legacy-alias/table/cache purge calls. This supplements them
    with ALL immutable generations and automatic state. Delete bytes first;
    failures leave controls/jobs available for the existing purge retry flow.
    No grace/retention applies to an explicitly quiesced destructive purge.
    """
    if not callable(quiesced_check):
        raise CleanupDeferred('A durable quiescence check is required')
    quiesced_check()
    blobs = purge_immutable_artifacts(storage, user, check=quiesced_check)
    rows = purge_automatic_state(storage, jobs, user, quiesced_check=quiesced_check)
    return dict(deletedBlobs=blobs, deletedAutomaticRows=rows)