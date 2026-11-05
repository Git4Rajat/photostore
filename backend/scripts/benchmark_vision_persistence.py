"""Isolated persistence prototypes; never imported by the application.

Uses production face writers, capture and completion acceptance. Only immutable
receipt rereads and bounded cross-image artifact writes are intercepted. No
model, blob promotion, wave orchestration or full-upload throughput is measured.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode
import processing_fence as fencing
from processing_completion import STEPS, prepare_completion, validate_intent
from processing_receipts import ArtifactReceipts
from upload_waves import ShadowWaves
import storage_utils as storage


@contextmanager
def patch(obj, name, value):
    previous = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield previous
    finally:
        setattr(obj, name, previous)


_receipts = ContextVar('benchmark_committed_receipts', default=None)


@contextmanager
def receipt_prototype(enabled):
    """Cache only successfully committed immutable payloads in this call scope."""
    if not enabled:
        yield
        return
    get, put, many = ArtifactReceipts._get, ArtifactReceipts._put, ArtifactReceipts._put_many
    def key(self, rowkey):
        return id(self.table), self.partition, rowkey
    def cached_get(self, rowkey):
        cache = _receipts.get()
        found = cache.get(key(self, rowkey)) if cache is not None else None
        return dict(found) if found is not None else get(self, rowkey)
    def remember(self, kind, payload):
        identity, row = self._prepare(kind, payload)
        cache = _receipts.get()
        if cache is not None:
            cache[key(self, row['RowKey'])] = row
        return identity
    def committed_put(self, kind, payload):
        identity = put(self, kind, payload)
        remember(self, kind, payload)  # Never populated before durable success.
        return identity
    def committed_many(self, kind, payloads):
        payloads = list(payloads)
        identities = many(self, kind, payloads)
        for payload in payloads:
            remember(self, kind, payload)
        return identities
    with patch(ArtifactReceipts, '_get', cached_get), patch(ArtifactReceipts, '_put', committed_put), \
            patch(ArtifactReceipts, '_put_many', committed_many):
        yield


@contextmanager
def receipt_scope():
    token = _receipts.set({})
    try:
        yield
    finally:
        _receipts.reset(token)


class CrossImageBatch:
    """Bounded queue; no caller may publish completion until flush succeeds.

    Keeps original artifact ETags, fresh per-source/lookup guards, embedding
    before face ordering, and complete lookup publication strictly last.
    """
    def __init__(self, guard_workers=1):
        self.operations, self.finishes = {}, []
        self.lock = threading.Lock()
        self.original_finish = storage._finish_face_filename_write
        self.original_renew = storage._renew_face_filename_write
        self.guard_workers = guard_workers
        self.timings = Counter()

    def enqueue(self, table, entity, *, previous=None, previous_known=False):
        fence = fencing.current_fence(entity['PartitionKey'], entity.get('filename'))
        if fence is None:
            raise ValueError('Source fence required')
        if not previous_known:
            try:
                previous = table.get_entity(entity['PartitionKey'], entity['RowKey'])
            except ResourceNotFoundError:
                previous = None
        row = dict(entity, **fence.headers())
        operation = ('create', row) if previous is None else ('update', row, dict(
            mode=UpdateMode.REPLACE, etag=previous.metadata['etag'], match_condition=MatchConditions.IfNotModified))
        with self.lock:
            self.operations.setdefault(table, []).append((operation, fence))

    def finish(self, *args, **kwargs):
        if args[3] is None:
            return self.original_finish(*args, **kwargs)
        with self.lock:
            self.finishes.append((args, kwargs, fencing.current_fence()))

    def renew(self, *args, **kwargs):
        # This preparation only queues writes. Flush performs fresh checks.
        if not kwargs.get('artifact_checks_source'):
            return self.original_renew(*args, **kwargs)

    @contextmanager
    def installed(self):
        with patch(fencing, 'write_artifact', self.enqueue), \
                patch(storage, '_finish_face_filename_write', self.finish), \
                patch(storage, '_renew_face_filename_write', self.renew):
            yield self

    def flush(self):
        generations = {(f.library, f.filename): args[2] for args, _, f in self.finishes}
        # Explicit ordering; thread scheduling must not determine table order.
        ordered = [storage._CTX['face_embeddings_table_client'], storage._CTX['face_table_client']]
        for table in ordered:
            entries = self.operations.get(table, [])
            # Conservative byte cap plus Azure's 100-operation transaction cap.
            pages, page, size = [], [], 0
            for entry in entries:
                estimate = len(json.dumps(entry[0][1], default=str).encode()) * 2 + 1024
                if page and (len(page) == 100 or size + estimate > 3_000_000):
                    pages.append(page); page, size = [], 0
                page.append(entry); size += estimate
            if page:
                pages.append(page)
            for page in pages:
                guards = {(f.library, f.filename): f for _, f in page}
                started = time.perf_counter()
                def check(item):
                    source, fence = item
                    with fencing.result_fence(fence):
                        self.original_renew(*source, generations[source], artifact_checks_source=True)
                        fence.check()
                if self.guard_workers > 1:
                    with ThreadPoolExecutor(self.guard_workers) as pool:
                        list(pool.map(check, guards.items()))
                else:
                    for item in guards.items():
                        check(item)
                self.timings['batch_guard_seconds'] += time.perf_counter()-started
                started = time.perf_counter()
                table.submit_transaction([operation for operation, _ in page])
                self.timings['batch_write_seconds'] += time.perf_counter()-started
        started = time.perf_counter()
        def finish(entry):
            args, kwargs, fence = entry
            with fencing.result_fence(fence):
                self.original_finish(*args, **kwargs)
        if self.guard_workers > 1:
            with ThreadPoolExecutor(self.guard_workers) as pool:
                list(pool.map(finish, self.finishes))
        else:
            for entry in self.finishes:
                finish(entry)
        self.timings['batch_lookup_publish_seconds'] += time.perf_counter()-started
        self.operations.clear(); self.finishes.clear()


class Meter:
    def __init__(self, table, name, counts, lock):
        self.table, self.name, self.counts, self.lock = table, name, counts, lock
    def __getattr__(self, name):
        fn = getattr(self.table, name)
        def call(*args, **kwargs):
            with self.lock:
                self.counts[self.name + '.' + name] += 1
            return fn(*args, **kwargs)
        return call


def configure(tables):
    storage._CTX.update(metadata_table_client=tables['metadata'], face_table_client=tables['faces'],
        face_embeddings_table_client=tables['embeddings'], face_by_filename_table_client=tables['lookup'],
        blob_service_client=None, person_table_client=None, person_members_table_client=None,
        face_summary_lookup=None, face_summary_cache_writer=None, source_policy_check=None)
    storage.touch_user_search_indexes_state = lambda *a, **k: None
    storage._start_face_crop_warming = lambda *a, **k: None


def inputs(count):
    return [dict(embedding=[((j+i)%31-15)/100 for j in range(512)], confidence=.9,
        bbox=dict(left=i*20, top=0, width=10, height=10), imageWidth=200, imageHeight=200,
        embeddingVersion='benchmark-v1') for i in range(count)]


def execute(raw, *, mode, face_count, images, group, workers=3, seeded=False, library=None):
    library = library or 'benchmark-' + uuid.uuid4().hex
    counts, lock = Counter(), threading.Lock()
    tables = {n: Meter(t, n, counts, lock) for n, t in raw.items()}
    configure(tables)
    rows, fences = [], []
    for i in range(images):
        filename = f'{i:04}.jpg'
        row = dict(PartitionKey=library, RowKey=filename, anonymousImageId=f'photo-{i}',
            processingRevision='rev', processingGeneration='b'*64, processingFence='attempt',
            processing_lease_owner='owner', processing_lease_expires_at=(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat(),
            faceCount=face_count, **{step+'_status': 'no_data' for step in STEPS})
        if face_count:
            row['face_status'] = 'done'
        tables['metadata'].create_entity(row)
        tables['lookup'].create_entity(dict(PartitionKey=library, RowKey=filename,
            schemaVersion=storage._FACE_FILENAME_SCHEMA_VERSION, generation=uuid.uuid4().hex,
            state='complete', faceIds='[]', leaseExpiresAt=''))
        fence = fencing.ResultFence(tables['metadata'], library, filename, 'rev', 'attempt', 'owner', f'photo-{i}', generation='b'*64)
        rows.append(row); fences.append(fence)
        if seeded:
            with fencing.result_fence(fence):
                ids = storage._store_client_face_entities(library, filename, inputs(face_count))
            for fid in ids:
                previous = tables['faces'].get_entity(library, fid)
                entity = dict(previous)
                entity.update(personId='curated-person', confirmedByUser=True, reviewStatus='confirmed')
                tables['faces'].update_entity(entity, mode=UpdateMode.REPLACE,
                    etag=previous.metadata['etag'], match_condition=MatchConditions.IfNotModified)
    counts.clear()
    phases, delivered = Counter(), {}
    def persist(i):
        with fencing.result_fence(fences[i]):
            storage._store_client_face_entities(library, rows[i]['RowKey'], inputs(face_count))
    def complete(i):
        row = dict(rows[i])
        with receipt_scope(), fencing.result_fence(fences[i]):
            previous = tables['metadata'].get_entity(library, row['RowKey'])
            with fencing.immutable_artifact_checks(fences[i]):
                reference = ArtifactReceipts(tables['jobs'], library).capture(storage, row)
            row['artifactReceipt'] = json.dumps(reference)
            row.update(processing_lease_owner='', processing_lease_expires_at='')
            prepare_completion(row)
            tables['metadata'].update_entity(row, mode=UpdateMode.REPLACE,
                etag=previous.metadata['etag'], match_condition=MatchConditions.IfNotModified)
            intent = json.loads(row['processingCompletionOutbox'])[0]
            accepted = ShadowWaves(tables['jobs'], library).accept_source_completion(intent)
            assert accepted['artifactReceiptQualified']
            with lock:
                delivered[i] = reference, intent, accepted
    start, cpu = time.perf_counter(), time.process_time()
    with ThreadPoolExecutor(workers) as pool, receipt_prototype(mode in ('receipts', 'combined', 'combined_parallel')):
        for offset in range(0, images, group):
            indices = list(range(offset, min(images, offset+group)))
            then = time.perf_counter()
            if mode in ('batch', 'combined', 'batch_parallel', 'combined_parallel'):
                with CrossImageBatch(workers if mode.endswith('_parallel') else 1).installed() as batch:
                    list(pool.map(persist, indices))
                    batch.flush()
                    phases.update(batch.timings)
            else:
                list(pool.map(persist, indices))
            phases['face_seconds'] += time.perf_counter()-then
            then = time.perf_counter()
            list(pool.map(complete, indices))
            phases['capture_delivery_seconds'] += time.perf_counter()-then
    wall, cpu = time.perf_counter()-start, time.process_time()-cpu
    # Readbacks outside timing, compare semantic output rather than random IDs.
    canonical = []
    for row in rows:
        lookup = raw['lookup'].get_entity(library, row['RowKey'])
        ids = json.loads(lookup['faceIds'])
        assert lookup['state'] == 'complete' and len(ids) == face_count
        values = []
        for fid in ids:
            face = raw['faces'].get_entity(library, fid)
            embedding = raw['embeddings'].get_entity(library, fid)
            values.append({k: face.get(k) for k in ('bbox', 'confidence', 'personId', 'confirmedByUser', 'reviewStatus')} |
                          dict(embedding=embedding.get('embedding')))
        canonical.append(sorted(values, key=lambda x: json.dumps(x, sort_keys=True)))
    for reference, intent, accepted in delivered.values():
        ArtifactReceipts(raw['jobs'], library).verify_reference(reference, intent)
        inbox = raw['jobs'].get_entity(accepted['PartitionKey'], accepted['RowKey'])
        assert inbox['artifactReceiptQualified'] and json.loads(inbox['payload']) == validate_intent(intent, library)
    return dict(mode=mode, faces=face_count, images=images, group=group, workers=workers, seeded=seeded,
        wall_seconds=wall, cpu_seconds=cpu, milliseconds_per_image=wall*1000/images,
        requests=sum(counts.values()), calls=dict(counts), phases=dict(phases),
        output_digest=hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest())


def main():
    from azure.data.tables import TableServiceClient
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument('--images', type=int, default=32)
    parser.add_argument('--parallel-followup', action='store_true')
    args = parser.parse_args()
    service = TableServiceClient.from_connection_string(os.environ['BENCHMARK_STORAGE_CONNECTION'])
    tables = {n: service.get_table_client('bench'+n) for n in ('metadata', 'faces', 'embeddings', 'lookup', 'jobs')}
    storage._LOGGER.setLevel('ERROR')
    reports, references = [], {}
    matrix = [(1, False, (1, 8, 16)), (0, False, (8,)), (8, False, (8,)), (1, True, (8,))]
    if args.parallel_followup:
        matrix = [(1, False, (8, 16))]
    for faces, seeded, groups in matrix:
        for group in groups:
            for round_number in range(args.rounds):
                modes = ['current', 'batch', 'receipts', 'combined']
                if args.parallel_followup:
                    modes = ['current', 'batch_parallel', 'receipts', 'combined_parallel']
                for mode in modes if round_number%2 == 0 else reversed(modes):
                    row = execute(tables, mode=mode, face_count=faces, seeded=seeded,
                        images=args.images, group=group)
                    key = faces, seeded
                    references.setdefault(key, row['output_digest'])
                    assert references[key] == row['output_digest'], 'Output mismatch'
                    row['round'] = round_number; reports.append(row)
                    args.output.write_text(json.dumps(dict(scope=__doc__, output_parity=True, reports=reports), indent=2))
                    print('ROUND '+json.dumps({k: row[k] for k in ('mode', 'faces', 'seeded', 'group', 'round', 'wall_seconds', 'requests')}), flush=True)


if __name__ == '__main__':
    main()
