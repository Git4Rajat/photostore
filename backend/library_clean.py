"""Clean library: delete everything one library owns, as fast as storage allows.

A direct admin action with its own code (see docs/library-clean-dedicated.md). It does not go through
the per-photo delete path (_clean_one_photo, _delete_photo_blobs_if_present) or tracked SourceTable
writes: rows go in 100-row entity-group transactions straight to the raw tables, blobs in 256-blob
batch deletes, and the whole library's index state is purged once at the end.

Everything a library can own is listed in REGISTRY. The wipe walks it phase by phase and the final
verification walks it again; tests/test_library_clean.py fails when an app table or container
constant has no entry, so a new store forces a clean decision.

The caller (app._execute_library_clean) holds the catalog lock and the library's index fence and has
already run processing_cleanup.begin_library_cleanup. `Stores.check` is that fence's proof; it is
rechecked once per round of writes instead of before every row, and no write starts after a failed
check.
"""
import hashlib
import json
import logging
import os
import random
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Callable, Dict, Optional

from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError, ServiceResponseError

LOG = logging.getLogger(__name__)

WORKERS = int(os.getenv('LIBRARY_CLEAN_WORKERS', '32'))
# Azure Tables allows ~2,000 entities/s per partition and a 100-op transaction counts as 100, so three
# transactions in flight per partition (~150 ms each) is about its limit; other partitions use the rest.
PARTITION_WORKERS = int(os.getenv('LIBRARY_CLEAN_PARTITION_WORKERS', '3'))
LISTERS = 8
PAGE = 1000
TRANSACTION = 100
BLOB_BATCH = 256
RETRIES = 6
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
PROGRESS_SECONDS = 10.0

# App constant name -> the store key REGISTRY uses. Every *_TABLE / BLOB_*_CONTAINER constant in app.py
# must be listed (tests enforce it), so a new store cannot be added without deciding how a clean treats it.
TABLE_CONSTANTS = {
    'METADATA_TABLE': 'metadata', 'EMBEDDINGS_TABLE': 'embeddings', 'FACE_EMBEDDINGS_TABLE': 'face_embeddings',
    'FACE_TABLE': 'faces', 'FACE_BY_FILENAME_TABLE': 'face_by_filename', 'PEOPLE_TABLE': 'people',
    'PERSON_MEMBERS_TABLE': 'person_members', 'ALBUMS_TABLE': 'albums', 'ALBUM_MEMBERS_TABLE': 'album_members',
    'ALBUM_TOKEN_INDEX_TABLE': 'album_tokens', 'MERGE_TABLE': 'merges', 'IMAGE_NAMES_TABLE': 'image_names',
    'HASH_INDEX_TABLE': 'hashes', 'FILENAME_OWNERS_TABLE': 'filename_owners', 'TRASH_INDEX_TABLE': 'trash',
    'WORKBENCH_ACTIONS_TABLE': 'workbench', 'JOBS_TABLE': 'jobs', 'SEARCH_INDEX_DIRTY_TABLE': 'search_dirty',
    'PEOPLE_DIRTY_SET_TABLE': 'people_dirty', 'ALBUMS_DIRTY_SET_TABLE': 'albums_dirty',
    'UPLOAD_SESSIONS_TABLE': 'upload_sessions', 'USERS_TABLE': 'users', 'LIBRARIES_TABLE': 'libraries',
    'MEMBERSHIPS_TABLE': 'memberships', 'INVITES_TABLE': 'invites', 'AUDIT_TABLE': 'audit',
    'CLEAN_REQUESTS_TABLE': 'clean_requests', 'CONFIG_TABLE': 'config',
}
CONTAINER_CONSTANTS = {
    'BLOB_IMAGE_CONTAINER': 'images', 'BLOB_THUMBNAIL_CONTAINER': 'thumbnails', 'BLOB_COVER_CONTAINER': 'covers',
    'BLOB_EXPORTS_CONTAINER': 'exports', 'BLOB_MERGE_PAYLOADS_CONTAINER': 'merge_payloads',
    'BLOB_VECTOR_INDEX_CONTAINER': 'vector_index', 'BLOB_LEXICAL_INDEX_CONTAINER': 'lexical_index',
    'BLOB_PEOPLE_EMBEDDING_INDEX_CONTAINER': 'people_embedding_index',
    'BLOB_TAG_EMBEDDING_INDEX_CONTAINER': 'tag_embedding_index',
    'WEB_CONTAINER': 'web',  # share_snapshot.WEB_CONTAINER, the static website
}


class Incomplete(RuntimeError):
    """Data was still left after a second pass: the job fails instead of reporting done."""

    def __init__(self, residue):
        super().__init__('Library clean left data behind: ' + json.dumps(residue, sort_keys=True))
        self.residue = residue


def sha256(text):
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def wave_digest(library):
    from upload_waves import digest
    return digest(library)


def raw(client):
    """The plain TableClient under a SourceTable: a clean is not an index source write."""
    return getattr(client, 'table', client)


def quote(value):
    return str(value).replace("'", "''")


def pages(items, size=PAGE):
    items = iter(items)
    while True:
        page = list(islice(items, size))
        if not page:
            return
        yield page


def listed(query):
    """Iterate a table query or blob listing. A table or container that does not exist yet (a fresh install
    creates some lazily) has nothing to delete; queries raise not-found for nothing else."""
    try:
        yield from query()
    except ResourceNotFoundError:
        return


def _transient(error):
    if isinstance(error, (ServiceRequestError, ServiceResponseError, TimeoutError, ConnectionError)):
        return True
    return getattr(error, 'status_code', None) in TRANSIENT_STATUS


def _not_found(error):
    return isinstance(error, ResourceNotFoundError) or getattr(error, 'status_code', None) == 404


@dataclass
class Stores:
    """What the clean touches, handed in by the app so this module imports nothing from it."""
    tables: Dict[str, Any]
    blobs: Any = None
    containers: Dict[str, str] = field(default_factory=dict)
    check: Callable[[], None] = lambda: None
    progress: Callable[[Dict], None] = lambda state: None
    # The app's index purges and caches, run once in phase 5; they own the index containers.
    finish_indexes: Callable[[], None] = lambda: None
    # share_snapshot.delete(container, token): removes a public album snapshot.
    delete_snapshot: Optional[Callable[[Any, str], int]] = None

    def table(self, key):
        client = self.tables.get(key)
        return raw(client) if client is not None else None

    def container(self, key):
        name = self.containers.get(key)
        if self.blobs is None or not name:
            return None
        return self.blobs.get_container_client(name)


# --- registry ----------------------------------------------------------------------------------------

class Entry:
    store = ''
    note = ''
    phase = None
    # Phase 2 entries whose locations are found through parent rows run before those parents go.
    before_parents = False

    def label(self):
        return f'{self.store}:{self.note}' if self.note else self.store

    def wipe(self, run):
        raise NotImplementedError

    def remaining(self, run):
        raise NotImplementedError


class Partition(Entry):
    """Every row of one partition whose key derives from the library (minus `keep`)."""

    def __init__(self, table, key, *, phase, keep=None, keep_fields=(), note=''):
        self.store, self.key, self.phase, self.note = table, key, phase, note
        self.keep, self.keep_fields = keep, tuple(keep_fields)

    def wipe(self, run):
        table = run.stores.table(self.store)
        if table is not None:
            run.wipe_partition(table, self.store, self.key(run.library), keep=self.keep, keep_fields=self.keep_fields)

    def remaining(self, run):
        table = run.stores.table(self.store)
        if table is None:
            return 0
        return run.count_partition(table, self.store, self.key(run.library), keep=self.keep,
                                   keep_fields=self.keep_fields)


class Children(Entry):
    """One partition per surveyed parent row (person -> members, album -> album members)."""
    before_parents = True

    def __init__(self, table, parents, *, note='', counted_by_sweep=False):
        self.store, self.parents, self.phase, self.note = table, parents, 2, note
        # Rows that carry the library id are counted by one table sweep instead of a query per parent.
        self.counted_by_sweep = counted_by_sweep

    def wipe(self, run):
        table = run.stores.table(self.store)
        if table is not None:
            list(run.listers.map(lambda pk: run.wipe_partition(table, self.store, pk),
                                 sorted(run.survey[self.parents])))

    def remaining(self, run):
        table = run.stores.table(self.store)
        if table is None or self.counted_by_sweep:
            return 0
        return sum(run.count_partition(table, self.store, pk) for pk in run.survey[self.parents])


class Payloads(Entry):
    """Merge payload blobs named by the library's merge rows."""
    store, note, phase, before_parents = 'merge_payloads', 'merge payloads', 2, True

    def wipe(self, run):
        run.wait(run.delete_names('merge_payloads', sorted(run.survey['payloads'])))

    def remaining(self, run):
        container = run.stores.container('merge_payloads')
        if container is None:
            return 0
        return sum(1 for name in run.survey['payloads'] if container.get_blob_client(name).exists())


class Tokens(Entry):
    """Public share links of the library's albums: the token index row (only when it names this
    library) and the static snapshot under $web/s/<token>/."""
    store, note, phase, before_parents = 'album_tokens+web', 'share links and snapshots', 2, True

    def wipe(self, run):
        table, web = run.stores.table('album_tokens'), run.stores.container('web')
        for token in sorted(run.survey['tokens']):
            if table is not None and self._owned(run, table, token):
                run.gate()
                run.call('point_delete', 'album_tokens', table.delete_entity, token, 'owner', missing_ok=True)
            if web is not None and run.stores.delete_snapshot is not None:
                run.gate()
                removed = run.call('snapshot_delete', 'web', run.stores.delete_snapshot, web, token,
                                   missing_ok=True) or 0
                with run.lock:
                    run.blobs_deleted += removed

    @staticmethod
    def _owned(run, table, token):
        try:
            row = run.call('point', 'album_tokens', table.get_entity, token, 'owner')
        except ResourceNotFoundError:
            return False
        return str(row.get('userId') or '') == run.library

    def remaining(self, run):
        table, web = run.stores.table('album_tokens'), run.stores.container('web')
        left = 0
        for token in run.survey['tokens']:
            if table is not None and self._owned(run, table, token):
                left += 1
            if web is not None:
                left += sum(1 for _ in islice(listed(lambda: web.list_blobs(name_starts_with=f's/{token}/')), 1))
        return left


class Rows(Entry):
    """Rows of shared partitions picked by a column, e.g. filename owners whose RowKey is the library."""

    def __init__(self, table, query, owned, *, phase, note=''):
        self.store, self.query, self.owned, self.phase, self.note = table, query, owned, phase, note

    def _rows(self, run, table):
        run.count('query', self.store)
        for row in listed(lambda: table.query_entities(self.query(run.library), select=['PartitionKey', 'RowKey', 'userId'],
                                                       results_per_page=PAGE)):
            if self.owned(row, run.library):
                yield row

    def wipe(self, run):
        table = run.stores.table(self.store)
        if table is not None:
            for page in pages(self._rows(run, table)):
                run.wait([run.submit(None, run.delete_rows, table, [row]) for row in page])

    def remaining(self, run):
        table = run.stores.table(self.store)
        return 0 if table is None else sum(1 for _ in islice(self._rows(run, table), PAGE))


class Point(Entry):
    """One row keyed by the library inside a shared partition."""

    def __init__(self, table, pk, rk, *, phase, note=''):
        self.store, self.pk, self.rk, self.phase, self.note = table, pk, rk, phase, note

    def wipe(self, run):
        table = run.stores.table(self.store)
        if table is not None:
            run.gate()
            run.call('point_delete', self.store, table.delete_entity, self.pk(run.library), self.rk(run.library),
                     missing_ok=True)

    def remaining(self, run):
        table = run.stores.table(self.store)
        if table is None:
            return 0
        try:
            run.call('point', self.store, table.get_entity, self.pk(run.library), self.rk(run.library))
            return 1
        except ResourceNotFoundError:
            return 0


class Prefix(Entry):
    """Blobs under a library-derived prefix."""

    def __init__(self, container, prefix, *, phase, note=''):
        self.store, self.prefix, self.phase, self.note = container, prefix, phase, note

    def wipe(self, run):
        container = run.stores.container(self.store)
        if container is not None:
            run.delete_prefix(container, self.store, self.prefix(run.library))

    def remaining(self, run):
        container = run.stores.container(self.store)
        if container is None:
            return 0
        run.count('list', self.store)
        return sum(1 for _ in islice(listed(lambda: container.list_blobs(name_starts_with=self.prefix(run.library))),
                                     PAGE))


class Photos(Entry):
    """Per-photo blobs and filename-owner rows, computed from each metadata row."""
    store, note, phase = 'images+thumbnails', 'per-photo blobs', 1

    def wipe(self, run):
        run.clean_photos()

    def remaining(self, run):
        # Metadata is gone by verification time; every blob that failed to delete is kept for retry.
        return len(run.failed_blobs)


class Keep(Entry):
    def __init__(self, store, reason):
        self.store, self.note = store, f'kept ({reason})'


def _library(lib):
    return lib


def _keep_clean_jobs(row):
    # This run's own job row is still 'running', and past library_clean rows are the audit trail.
    return row.get('jobType') == 'library_clean'


def _keep_migration(row):
    # The upload-session migration cursor: dropping it would re-run that migration.
    return row.get('RowKey') == 'migration'


REGISTRY = [
    # Phase 1: per-photo blobs before the metadata rows that name them.
    Photos(),
    # Phase 2: child locations first (surveyed from their parents), then the library's own partitions.
    Children('person_members', 'people', note='members of each person', counted_by_sweep=True),
    Children('album_members', 'albums', note='members of each album'),
    Tokens(),
    Payloads(),
    Partition('embeddings', _library, phase=2),
    Partition('face_embeddings', _library, phase=2),
    Partition('face_by_filename', _library, phase=2),
    Partition('faces', _library, phase=2),
    Partition('people', _library, phase=2),
    Partition('albums', _library, phase=2),
    Partition('merges', _library, phase=2),
    Partition('image_names', _library, phase=2),
    Partition('hashes', _library, phase=2),
    Partition('trash', _library, phase=2),
    Partition('workbench', _library, phase=2),
    Partition('people_dirty', _library, phase=2),
    Partition('albums_dirty', _library, phase=2),
    Partition('jobs', _library, phase=2, keep=_keep_clean_jobs, keep_fields=('jobType',), note='job history'),
    # Phase 3: partitions and prefixes keyed by a hash of the library.
    Partition('people_dirty', lambda lib: 'labels:' + sha256(lib), phase=3, note='label dirty set'),
    Partition('jobs', lambda lib: 'processing-artifacts-' + wave_digest(lib), phase=3,
              note='face snapshots and manifests'),
    Partition('upload_sessions', lambda lib: 'library-' + wave_digest(lib), phase=3, keep=_keep_migration,
              note='upload session registry'),
    Prefix('thumbnails', lambda lib: f'derived/v1/{sha256(lib)}/', phase=3, note='published derivatives'),
    Prefix('thumbnails', lambda lib: f'processing-staging/v1/{sha256(lib)}/', phase=3, note='staging derivatives'),
    Prefix('covers', lambda lib: sha256(lib)[:16] + '/', phase=3, note='face covers'),
    Prefix('exports', lambda lib: f'{lib}/', phase=3, note='library export zips'),
    # Phase 4: metadata last (it is how every per-photo location is found), then orphan sweeps that catch
    # rows whose photo or person row was already gone. Both sweeps scan their whole table once.
    Partition('metadata', _library, phase=4),
    Point('metadata', lambda lib: 'clustering_maintenance', _library, phase=4, note='clustering activity'),
    Rows('filename_owners', lambda lib: f"RowKey eq '{quote(lib)}'", lambda row, lib: row.get('RowKey') == lib,
         phase=4, note='filename owners'),
    Rows('person_members', lambda lib: f"userId eq '{quote(lib)}'", lambda row, lib: row.get('userId') == lib,
         phase=4, note='orphaned person members'),
    # Phase 5: index state goes through the app's own purges (Stores.finish_indexes), plus the one blob
    # those purges miss.
    Prefix('people_embedding_index', lambda lib: f'faiss-source-revisions/{sha256(lib)}', phase=5,
           note='FAISS source revision'),
    # Each upload wave copies the published index artifacts it proves under wave-projections/<digest(library)>/
    # (wave_catalog._copy). The index purges above do not know that prefix, so a clean left every copy behind.
    *[Prefix(container, lambda lib: f'wave-projections/{wave_digest(lib)}/', phase=5, note='wave projection copies')
      for container in ('vector_index', 'lexical_index', 'people_embedding_index', 'tag_embedding_index')],
    Keep('vector_index', 'delete_user_vector_index_data, purge_immutable_artifacts'),
    Keep('lexical_index', 'delete_user_lexical_index_data, purge_immutable_artifacts'),
    Keep('tag_embedding_index', 'delete_user_tag_embedding_index_data, purge_immutable_artifacts'),
    Keep('search_dirty', 'automatic-<sha256> coverage rows go in purge_automatic_state'),
    # Deliberately left alone.
    Keep('jobs:upload-wave-shadow', 'wave history holds the deletion proofs late writers settle against'),
    Keep('users', 'people stay'), Keep('libraries', 'the library stays'), Keep('memberships', 'members stay'),
    Keep('invites', 'invitations stay'), Keep('audit', 'audit trail'), Keep('clean_requests', 'request history'),
    Keep('config', 'install configuration'),
]


# --- the run -----------------------------------------------------------------------------------------

class Run:
    def __init__(self, library, stores, *, workers=WORKERS):
        if not library:
            raise ValueError('Library clean needs a library id')
        self.library, self.stores, self.workers = library, stores, workers
        # Writes run on `pool`; listing runs on `listers`, so a write never waits on work queued behind it.
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='library-clean')
        self.listers = ThreadPoolExecutor(max_workers=LISTERS, thread_name_prefix='library-clean-list')
        self.slots = defaultdict(lambda: threading.BoundedSemaphore(PARTITION_WORKERS))
        self.lock = threading.Lock()
        self.since_check = workers  # so the first write checks
        self.calls = Counter()
        self.photos = self.rows_deleted = self.blobs_deleted = 0
        self.failed_blobs = {}  # (container key, name) -> last error, retried next pass
        self.survey = {'people': set(), 'albums': set(), 'tokens': set(), 'payloads': set()}
        self.phase = 'start'
        self.phase_seconds = {}
        self.last_progress = 0.0

    def close(self):
        self.listers.shutdown(wait=True)
        self.pool.shutdown(wait=True)

    # -- plumbing --

    def count(self, op, store):
        with self.lock:
            self.calls[(op, store)] += 1

    def call(self, op, store, fn, *args, missing_ok=False, **kwargs):
        """One storage call, retried with backoff on throttling, timeouts and connection errors."""
        for attempt in range(RETRIES):
            self.count(op, store)
            try:
                return fn(*args, **kwargs)
            except Exception as error:
                if missing_ok and _not_found(error):
                    return None
                if attempt + 1 == RETRIES or not _transient(error):
                    raise
                time.sleep(min(8.0, 0.2 * 2 ** attempt) * random.uniform(0.5, 1.5))

    def gate(self, force=False):
        """Recheck the fence proof once per round of writes (one round = `workers` writes) and at every
        phase boundary. Raises -- and so stops the clean -- when the proof no longer holds."""
        with self.lock:
            due = force or self.since_check >= self.workers
            self.since_check = 1 if due else self.since_check + 1
        if due:
            self.count('proof', 'fence')
            self.stores.check()

    def submit(self, slot, fn, *args):
        """Queue a write. Writes sharing a `slot` (one table partition) run at most PARTITION_WORKERS at once."""
        if slot is None:
            return self.pool.submit(fn, *args)
        semaphore = self.slots[slot]

        def task():
            with semaphore:
                return fn(*args)
        return self.pool.submit(task)

    @staticmethod
    def wait(futures):
        return sum(future.result() or 0 for future in futures)

    def state(self):
        return {'phase': self.phase, 'photos': self.photos, 'rowsDeleted': self.rows_deleted,
                'blobsDeleted': self.blobs_deleted, 'storageCalls': sum(self.calls.values())}

    def report(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_progress < PROGRESS_SECONDS:
            return
        self.last_progress = now
        try:
            self.stores.progress(self.state())
        except Exception:
            LOG.warning('Library clean progress report failed', exc_info=True)

    # -- tables --

    def delete_rows(self, table, rows):
        """Up to 100 rows of one partition in one transaction. A failed transaction is split in half down
        to single deletes, so one bad row never blocks the rest; a row already gone counts as deleted."""
        if not rows:
            return 0
        self.gate()
        if len(rows) == 1:
            self.call('delete', 'table', table.delete_entity, rows[0]['PartitionKey'], rows[0]['RowKey'],
                      missing_ok=True)
        else:
            try:
                self.call('batch', 'table', table.submit_transaction,
                          [('delete', {'PartitionKey': r['PartitionKey'], 'RowKey': r['RowKey']}) for r in rows])
            except Exception:
                half = len(rows) // 2
                return self.delete_rows(table, rows[:half]) + self.delete_rows(table, rows[half:])
        with self.lock:
            self.rows_deleted += len(rows)
        return len(rows)

    def _partition_rows(self, table, store, pk, keep, keep_fields):
        self.count('query', store)
        for row in listed(lambda: table.query_entities(f"PartitionKey eq '{quote(pk)}'",
                                                       select=['PartitionKey', 'RowKey', *keep_fields],
                                                       results_per_page=PAGE)):
            if not (keep and keep(row)):
                yield row

    def wipe_partition(self, table, store, pk, *, keep=None, keep_fields=()):
        """Delete one partition page by page in one pass. With the fence up nothing new arrives, and
        continuation tokens are key-ordered, so deleting behind the cursor skips nothing; the final
        verification (and its second pass) is the safety net."""
        futures = []
        for page in pages(self._partition_rows(table, store, pk, keep, keep_fields)):
            futures += [self.submit((store, pk), self.delete_rows, table, page[i:i + TRANSACTION])
                        for i in range(0, len(page), TRANSACTION)]
            self.report()
        return self.wait(futures)

    def count_partition(self, table, store, pk, *, keep=None, keep_fields=()):
        return sum(1 for _ in islice(self._partition_rows(table, store, pk, keep, keep_fields), PAGE))

    # -- blobs --

    def delete_blobs(self, container, store, names):
        """Up to 256 names of one container in one batch request. A batch that fails as a whole is split
        in half and retried; a name the service refuses is retried alone and remembered if it still fails."""
        if not names:
            return 0
        self.gate()
        if len(names) == 1:
            deleted = self._delete_blob(container, store, names[0])
        else:
            try:
                responses = list(self.call('blob_batch', store, container.delete_blobs, *names,
                                           delete_snapshots='include', raise_on_any_failure=False))
            except Exception:
                half = len(names) // 2
                return (self.delete_blobs(container, store, names[:half])
                        + self.delete_blobs(container, store, names[half:]))
            deleted = 0
            for name, response in zip(names, responses):
                status = getattr(response, 'status_code', 202)
                if status == 202:
                    deleted += 1
                elif status != 404:
                    deleted += self._delete_blob(container, store, name)
        with self.lock:
            self.blobs_deleted += deleted
        return deleted

    def _delete_blob(self, container, store, name):
        try:
            self.call('blob_delete', store, container.delete_blob, name, delete_snapshots='include')
            deleted = 1
        except Exception as error:
            if not _not_found(error):
                with self.lock:
                    self.failed_blobs[(store, name)] = str(error)[:200]
                return 0
            deleted = 0
        with self.lock:
            self.failed_blobs.pop((store, name), None)
        return deleted

    def delete_names(self, store, names):
        """Queue batch deletes for named blobs of one container; returns the futures."""
        container = self.stores.container(store)
        if container is None or not names:
            return []
        names = list(dict.fromkeys(names))
        return [self.pool.submit(self.delete_blobs, container, store, names[i:i + BLOB_BATCH])
                for i in range(0, len(names), BLOB_BATCH)]

    def delete_prefix(self, container, store, prefix):
        """Every blob under a prefix, listed once (listing is name-ordered, so deleting behind it is safe)."""
        self.count('list', store)
        futures = []
        for page in pages((blob.name for blob in listed(lambda: container.list_blobs(name_starts_with=prefix))),
                          5000):
            futures += [self.pool.submit(self.delete_blobs, container, store, page[i:i + BLOB_BATCH])
                        for i in range(0, len(page), BLOB_BATCH)]
        return self.wait(futures)

    # -- phase 1 --

    def clean_photos(self):
        """Per metadata page: every per-photo blob in batches and this library's filename-owner rows.
        Anonymized blob names belong to this library alone (anonymous ids are per library); a blob stored
        under the plain filename (pre-anonymization uploads) goes only when no other library owns that
        filename. Metadata rows stay until phase 4 so a retry can find every blob again."""
        if self.failed_blobs:  # a previous pass's failures; their metadata may already be gone
            by_store = defaultdict(list)
            for store, name in list(self.failed_blobs):
                by_store[store].append(name)
            self.wait([f for store, names in by_store.items() for f in self.delete_names(store, names)])
        metadata, owners = self.stores.table('metadata'), self.stores.table('filename_owners')
        if metadata is None:
            return
        self.count('query', 'metadata')
        rows = listed(lambda: metadata.query_entities(f"PartitionKey eq '{quote(self.library)}'",
                                                      select=['PartitionKey', 'RowKey', 'anonymousImageId'],
                                                      results_per_page=PAGE))
        for page in pages(rows):
            images, thumbnails, futures = [], [], []
            legacy = [str(r['RowKey']) for r in page if r.get('RowKey') and not str(r.get('anonymousImageId') or '').strip()]
            shared = set()
            if legacy and owners is not None:
                shared = {name for name, is_shared in zip(legacy, self.pool.map(
                    lambda name: self._filename_shared(owners, name), legacy)) if is_shared}
            for row in page:
                name = str(row.get('RowKey') or '')
                if not name:
                    continue
                anon = str(row.get('anonymousImageId') or '').strip()
                physical = [anon] if anon else ([] if name in shared else [name])
                if owners is not None:
                    futures.append(self.submit(None, self._delete_owner, owners, name))
                for blob in physical:
                    images.append(blob)
                    thumbnails += [blob, f'metadata/{blob}.bin', f'grid/{blob}.webp', f'preview/{blob}.jpg']
            futures += self.delete_names('images', images) + self.delete_names('thumbnails', thumbnails)
            self.wait(futures)
            with self.lock:
                self.photos += len(page)
            self.report()

    def _filename_shared(self, owners, name):
        rows = self.call('query', 'filename_owners', lambda: list(listed(lambda: owners.query_entities(
            f"PartitionKey eq '{quote(name)}'", select=['RowKey']))))
        return any(str(row.get('RowKey') or '') != self.library for row in rows)

    def _delete_owner(self, owners, name):
        self.gate()
        self.call('point_delete', 'filename_owners', owners.delete_entity, name, self.library, missing_ok=True)
        with self.lock:
            self.rows_deleted += 1
        return 0

    # -- phases --

    def take_survey(self):
        """Read the parents of child locations before the parents are deleted. Kept across passes, so the
        verification still knows every child partition."""
        lib = quote(self.library)
        people, albums, merges = (self.stores.table(key) for key in ('people', 'albums', 'merges'))
        def rows(table, fields):
            self.count('query', 'survey')
            return listed(lambda: table.query_entities(f"PartitionKey eq '{lib}'", select=fields,
                                                       results_per_page=PAGE))
        if people is not None:
            self.survey['people'].update(str(row['RowKey']) for row in rows(people, ['RowKey']))
        if albums is not None:
            for row in rows(albums, ['RowKey', 'publicToken', 'shareSnapshotToken']):
                self.survey['albums'].add(str(row['RowKey']))
                self.survey['tokens'].update(token for token in (str(row.get(f) or '').strip()
                                                                 for f in ('publicToken', 'shareSnapshotToken')) if token)
        if merges is not None:
            self.survey['payloads'].update(str(row['payloadBlobName']) for row in rows(merges, ['RowKey', 'payloadBlobName'])
                                           if row.get('payloadBlobName'))
        tokens = self.stores.table('album_tokens')
        if tokens is not None:
            # Token rows outlive albums an earlier clean deleted; they still name the library, so a rerun on an
            # emptied library still finds (and unpublishes) those public snapshots. One scan of a small table.
            self.count('query', 'survey')
            self.survey['tokens'].update(str(row['PartitionKey']) for row in listed(lambda: tokens.query_entities(
                f"userId eq '{lib}'", select=['PartitionKey', 'userId'], results_per_page=PAGE))
                if row.get('userId') == self.library)

    def run_phase(self, number):
        self.phase = f'phase{number}'
        started = time.monotonic()
        self.gate(force=True)
        entries = [entry for entry in REGISTRY if entry.phase == number]
        if number == 2:
            self.take_survey()
            for entry in entries:
                if entry.before_parents:
                    entry.wipe(self)
            entries = [entry for entry in entries if not entry.before_parents]
        if number == 5:
            self.stores.finish_indexes()
        # A phase's independent stores are listed in parallel; their writes share the bounded pool.
        list(self.listers.map(lambda entry: entry.wipe(self), entries))
        self.gate(force=True)
        self.phase_seconds[self.phase] = round(self.phase_seconds.get(self.phase, 0) + time.monotonic() - started, 2)
        self.report(force=True)

    def verify(self):
        residue = {}
        for entry in REGISTRY:
            if entry.phase is None:
                continue
            left = entry.remaining(self)
            if left:
                residue[entry.label()] = left
        return residue


# --- orphan blob sweep -------------------------------------------------------------------------------

ORPHAN_MIN_AGE = 7 * 24 * 3600
# The names one photo id takes in the thumbnails container (clean_photos deletes the same four).
_DERIVATIVES = (('metadata/', '.bin'), ('grid/', '.webp'), ('preview/', '.jpg'))


def photo_blob_id(name):
    """The photo id a blob name belongs to, or None for a name that is not per-photo (derived/..., staging, ...)."""
    for prefix, suffix in _DERIVATIVES:
        if name.startswith(prefix) and name.endswith(suffix):
            return name[len(prefix):-len(suffix)]
    return name if name and '/' not in name else None


def referenced_photo_ids(stores):
    """Every photo id any library's metadata row names (anonymized id, or the filename for legacy rows)."""
    metadata = stores.table('metadata')
    ids = set()
    if metadata is None:
        return ids
    # Non-photo partitions (system rows) only add names no blob has, so nothing is skipped.
    for row in listed(lambda: metadata.list_entities(select=['RowKey', 'anonymousImageId'], results_per_page=PAGE)):
        ids.add(str(row.get('anonymousImageId') or '').strip() or str(row.get('RowKey') or ''))
    return ids


def find_orphan_blobs(stores, *, older_than=ORPHAN_MIN_AGE, now=None):
    """(container key, blob name) of per-photo blobs no metadata row names and that are older than `older_than`
    seconds. A clean finds a photo's blobs through its metadata row, so blobs whose row is already gone (an
    interrupted upload, an earlier partial clean) are out of its reach; this finds them. The age floor keeps
    blobs of uploads still writing their rows, and an unknown age is never an orphan. It reads every library's
    metadata, so run it as an operator action, not inside one library's clean."""
    now = time.time() if now is None else now
    referenced = referenced_photo_ids(stores)
    found = []
    for key in ('images', 'thumbnails'):
        container = stores.container(key)
        if container is None:
            continue
        for blob in listed(container.list_blobs):
            modified = getattr(blob, 'last_modified', None)
            blob_id = photo_blob_id(blob.name)
            if modified is None or blob_id is None or blob_id in referenced:
                continue
            if now - modified.timestamp() >= older_than:
                found.append((key, blob.name))
    return found


def sweep_orphan_blobs(stores, *, older_than=ORPHAN_MIN_AGE, delete=False, now=None):
    """Count the orphan blobs per container; with delete=True also remove them in batch requests, checking the
    caller's fence proof before each batch."""
    orphans = find_orphan_blobs(stores, older_than=older_than, now=now)
    counts = Counter(key for key, _ in orphans)
    if delete:
        by_container = defaultdict(list)
        for key, name in orphans:
            by_container[key].append(name)
        for key, names in by_container.items():
            container = stores.container(key)
            for page in pages(names, BLOB_BATCH):
                stores.check()
                container.delete_blobs(*page, delete_snapshots='include', raise_on_any_failure=False)
    return dict(counts)


def run(library_id, stores, *, workers=WORKERS):
    """Wipe `library_id` and prove it is gone. Returns the job summary; raises Incomplete with the residue
    when a second pass still leaves data, so the job fails instead of reporting done."""
    started = time.monotonic()
    job = Run(library_id, stores, workers=workers)
    try:
        residue = {}
        for passes in (1, 2):
            for number in (1, 2, 3, 4, 5):
                job.run_phase(number)
            job.phase = 'verify'
            residue = job.verify()
            if not residue:
                break
            LOG.warning('Library clean pass %d left %s for %r', passes, residue, library_id)
        if residue:
            raise Incomplete(residue)
        job.phase = 'done'
        job.report(force=True)
        return {'photosDeleted': job.photos, 'blobsDeleted': job.blobs_deleted, 'blobErrors': 0,
                'rowsDeleted': job.rows_deleted, 'storageCalls': sum(job.calls.values()), 'passes': passes,
                'phaseSeconds': job.phase_seconds, 'seconds': round(time.monotonic() - started, 2)}
    finally:
        job.close()
