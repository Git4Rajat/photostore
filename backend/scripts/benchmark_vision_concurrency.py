"""Matched 2/3-worker Azure component benchmark; production data is never used.

Real models, semantic encoding and production face persistence use private test
tables. Optional residual apply waits are simulated, explicitly not full uploads.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import threading
import time
import uuid

from benchmark_vision_stages import cpu_stat, fingerprint


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=4)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--residual-wait-ms', type=float, default=260)
    args = parser.parse_args()
    if args.rounds < 2 or args.repeat < 1 or not 0 <= args.residual_wait_ms <= 1000:
        parser.error('At least two rounds, positive repeats and bounded residual wait required')
    from azure.data.tables import TableServiceClient
    import processing_metrics as metrics
    from processing_fence import ResultFence, result_fence
    from image_utils import convert_image_to_jpeg
    from ipwork_image import image_job
    import ipwork_face as face
    import ipwork_ocr as ocr
    import ipwork_vision as vision
    import storage_utils as storage
    import vision_utils
    storage._LOGGER.setLevel('ERROR')
    import processing_diagnostics
    processing_diagnostics._LOGGER.setLevel('ERROR') if hasattr(processing_diagnostics, '_LOGGER') else None
    service = TableServiceClient.from_connection_string(os.environ['BENCHMARK_STORAGE_CONNECTION'])
    tables = {name: service.get_table_client('bench' + name) for name in ('metadata', 'faces', 'embeddings', 'lookup')}
    storage._CTX.update(metadata_table_client=tables['metadata'], face_table_client=tables['faces'],
        face_embeddings_table_client=tables['embeddings'], face_by_filename_table_client=tables['lookup'],
        blob_service_client=None, person_table_client=None, person_members_table_client=None,
        face_summary_lookup=None, face_summary_cache_writer=None, source_policy_check=None)
    # These unrelated side effects cannot contact external services in this job.
    storage.touch_user_search_indexes_state = lambda *a, **k: None
    storage._start_face_crop_warming = lambda *a, **k: None
    face.FACE_LANDMARKER_MODEL_PATH = '/app/models/face_landmarker.task'
    samples = []
    for row in json.loads(args.manifest.read_text()):
        path = Path(row['path'])
        name = row.get('filename', path.name)
        data = convert_image_to_jpeg(path.read_bytes(), name)
        if not data: raise RuntimeError('Preview conversion failed')
        samples.append((name, data))
    if not samples or not vision_utils.image_encoder_available() or not vision._load_vocabulary():
        raise RuntimeError('Real models and nonempty inputs required')
    references, lock = {}, threading.Lock()

    def models(index):
        name, data = samples[index]
        results = {}
        with image_job(True):
            for step, fn in [('ocr', ocr.process_ocr), ('face', face.process_face), ('clip', vision.process_vision)]:
                results[step] = fn('benchmark', name, data)
                if results[step].get('error') or results[step].get('faceFailureStage') not in (None, '', 'quality_filter_rejected'):
                    raise RuntimeError('Model failure in ' + step)
        if not results['face']['faceDiagnostics']['detectorModelReady']:
            raise RuntimeError('Real face detector required')
        return results

    # Warm the exact resident OCR/model pools; all later variants use this process.
    with ThreadPoolExecutor(3) as pool:
        warmed = list(pool.map(models, range(len(samples))))
    face_indices = [i for i, result in enumerate(warmed) if any(
        storage._client_face_passes_quality_gate(f) for f in result['face'].get('faces', []))]
    if not face_indices: raise RuntimeError('Face-positive profile is empty')
    reports = []
    for profile, indices in [('mixed', list(range(len(samples)))), ('face_positive', face_indices)]:
        indices = indices * args.repeat
        for scenario in ('core', 'core_semantic_face_storage', 'residual_wait_replay'):
            for round_number in range(args.rounds):
                for workers in ((2, 3) if round_number % 2 == 0 else (3, 2)):
                    partition = 'b' + uuid.uuid4().hex
                    seeded = []
                    for position, index in enumerate(indices):
                        seeded.append(dict(PartitionKey=partition, RowKey=f'{position:05}.jpg',
                            anonymousImageId=f'photo-{position}', processingRevision='rev',
                            processingGeneration='b' * 64, processingFence='attempt',
                            processing_lease_owner='owner', processing_lease_expires_at=
                                (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()))
                    if scenario != 'core':
                        for start in range(0, len(seeded), 100):
                            group = seeded[start:start + 100]
                            tables['metadata'].submit_transaction([('create', row) for row in group])
                            # Equivalent to a completed empty filename-index preparation.
                            tables['lookup'].submit_transaction([('create', dict(PartitionKey=partition,
                                RowKey=row['RowKey'], schemaVersion=storage._FACE_FILENAME_SCHEMA_VERSION,
                                generation=uuid.uuid4().hex, state='complete', faceIds='[]', leaseExpiresAt='')) for row in group])
                    def process(position):
                        index = indices[position]
                        with metrics.accounting() as counter:
                            began = time.perf_counter()
                            result = models(index)
                            model_ms = (time.perf_counter() - began) * 1000
                            digest = fingerprint(result)
                            with lock:
                                previous = references.setdefault(index, digest)
                                if previous != digest: raise RuntimeError('Model output parity changed')
                            semantic_ms, storage_ms, saved = 0., 0., 0
                            semantic_digest = None
                            if scenario != 'core':
                                began = time.perf_counter()
                                text = ' '.join(map(str, result['clip'].get('tags', []))) + ' ' + str(result['ocr'].get('text') or '')
                                vector = vision_utils.encode_text_embedding(text)
                                if len(vector) != 512: raise RuntimeError('Real semantic encoding required')
                                semantic_digest = fingerprint(vector)
                                with lock:
                                    key = ('semantic', index)
                                    if references.setdefault(key, semantic_digest) != semantic_digest:
                                        raise RuntimeError('Semantic output parity changed')
                                semantic_ms = (time.perf_counter() - began) * 1000
                                row = seeded[position]
                                fence = ResultFence(tables['metadata'], partition, row['RowKey'], 'rev',
                                    'attempt', 'owner', row['anonymousImageId'], generation='b' * 64)
                                began = time.perf_counter()
                                with result_fence(fence):
                                    ids = storage._store_client_face_entities(partition, row['RowKey'], result['face'].get('faces', []))
                                saved = len(ids)
                                storage_ms = (time.perf_counter() - began) * 1000
                                if saved != sum(storage._client_face_passes_quality_gate(f) for f in result['face'].get('faces', [])):
                                    raise RuntimeError('Persisted face count changed')
                                if scenario == 'residual_wait_replay': time.sleep(args.residual_wait_ms / 1000)
                            return dict(model_ms=model_ms, semantic_ms=semantic_ms, storage_ms=storage_ms,
                                saved=saved, face_count=len(result['face'].get('faces', [])),
                                model_digest=digest, semantic_digest=semantic_digest, metrics=counter.snapshot())
                    before, cpu = cpu_stat(), time.process_time()
                    began = time.perf_counter()
                    with ThreadPoolExecutor(workers) as pool:
                        results = list(pool.map(lambda position: copy_context().run(process, position), range(len(indices))))
                    wall = time.perf_counter() - began
                    cpu_used = time.process_time() - cpu
                    after = cpu_stat()
                    # Verify durable filename membership and embedding presence outside timing.
                    if scenario != 'core':
                        for position, expected in enumerate(results):
                            row = tables['lookup'].get_entity(partition, seeded[position]['RowKey'])
                            ids = json.loads(row['faceIds'])
                            if row['state'] != 'complete' or len(ids) != expected['saved']:
                                raise RuntimeError('Incomplete persisted lookup')
                            for identity in ids:
                                f = tables['faces'].get_entity(partition, identity)
                                e = tables['embeddings'].get_entity(partition, identity)
                                if f['processingRevision'] != 'rev' or e['processingRevision'] != 'rev':
                                    raise RuntimeError('Persisted source binding changed')
                    report = dict(profile=profile, scenario=scenario, round=round_number, workers=workers,
                        images=len(indices), wall_seconds=wall, process_cpu_seconds=cpu_used,
                        images_per_hour=3600 * len(indices) / wall,
                        rss_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                        cgroup_delta={k: after[k] - before.get(k, 0) for k in after},
                        mean_model_ms=statistics.mean(r['model_ms'] for r in results),
                        mean_semantic_ms=statistics.mean(r['semantic_ms'] for r in results),
                        mean_storage_ms=statistics.mean(r['storage_ms'] for r in results),
                        output_digest=fingerprint([(r['model_digest'], r['semantic_digest'], r['saved']) for r in results]),
                        metrics={k: sum(r['metrics'].get(k, 0) for r in results) for k in results[0]['metrics']})
                    reports.append(report)
                    args.output.write_text(json.dumps(dict(scope=__doc__, output_parity=True,
                        residual_wait_ms=args.residual_wait_ms, samples=len(samples), face_positive_samples=len(face_indices),
                        cpu_max=Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
                        source_hashes={name: hashlib.sha256(Path('/app',name).read_bytes()).hexdigest()
                            for name in ('storage_utils.py','vision_utils.py','face_write_batch.py','ipwork_face.py','ipwork_ocr.py')},
                        reports=reports), indent=2))
                    print('ROUND ' + json.dumps({k: report[k] for k in ('profile','scenario','round','workers','images','wall_seconds','images_per_hour')}), flush=True)


if __name__ == '__main__':
    main()
