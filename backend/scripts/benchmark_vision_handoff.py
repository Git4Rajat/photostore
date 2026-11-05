"""Replay real inspection reads alongside matched offline model processing.

Historical wave keys supply read pressure; the independent 48-image validation
corpus supplies model work. This is a matched phase replay, not execution of
those historical photos or source commits. Production access is read-only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import time

from azure.data.tables import TableClient
from benchmark_vision_reads import TokenCredential
from benchmark_vision_stages import fingerprint
from table_reads import key_batches
from upload_waves import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--face-landmarker', required=True)
    parser.add_argument('--rounds', type=int, default=3)
    args = parser.parse_args()
    if args.rounds < 2:
        parser.error('Use at least two alternating rounds')
    from image_utils import convert_image_to_jpeg
    from ipwork_image import image_job
    import ipwork_face as face
    import ipwork_ocr as ocr
    import ipwork_vision as vision
    import vision_utils
    face.FACE_LANDMARKER_MODEL_PATH = args.face_landmarker
    if not vision_utils.image_encoder_available() or not vision._load_vocabulary():
        raise RuntimeError('Real models required')
    samples = []
    for item in json.loads(args.manifest.read_text()):
        path = Path(item['path'])
        name = item.get('filename', path.name)
        samples.append((name, convert_image_to_jpeg(path.read_bytes(), name)))
    table = TableClient('https://forenklaprep1devmqwhngus.table.core.windows.net', 'photojobs', credential=TokenCredential())
    partition = 'upload-wave-shadow-' + digest('owner')
    prefix = 'e!42cc98bb4e854c48a03a22593b654d59!m!'
    members = list(islice(table.query_entities("PartitionKey eq '" + partition + "' and RowKey ge '" + prefix +
                                             "' and RowKey lt '" + prefix + "~'"), len(samples)))
    if len(members) != len(samples):
        raise RuntimeError('Matched inspection groups unavailable')
    groups = [members[start:start + 20] for start in range(0, len(members), 20)]
    def inspect(group, grouped):
        started = time.perf_counter()
        keys = [kind + digest([member['photoId'], member['processingRevision']])
                for member in group for kind in ('deleted-source!', 'supersession!')]
        batches = [batch for kind in ('deleted-source!', 'supersession!')
                   for batch in key_batches([key for key in keys if key.startswith(kind)])] if grouped else list(key_batches(keys))
        result = dict.fromkeys(keys)
        calls = 0
        for batch in batches:
            wanted = set(batch)
            query = "PartitionKey eq '" + partition + "' and (" + ' or '.join("RowKey eq '" + key + "'" for key in batch) + ')'
            for row in table.query_entities(query):
                if row['RowKey'] not in wanted:
                    raise RuntimeError('Unexpected inspection key')
                result[row['RowKey']] = dict(row)
            calls += 1
        return dict(seconds=time.perf_counter() - started, requests=calls,
                    hash=hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest())
    def process(index):
        name, data = samples[index]
        result = {}
        with image_job(True):
            for stage, function in [('ocr', ocr.process_ocr), ('face', face.process_face), ('clip', vision.process_vision)]:
                result[stage] = function('offline-benchmark', name, data)
                if result[stage].get('error') or result[stage].get('faceFailureStage') not in (None, '', 'quality_filter_rejected'):
                    raise RuntimeError('Model failure: ' + stage)
        return fingerprint(result)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(process, range(min(12, len(samples)))))
    reports, reference, inspection_reference = [], None, None
    modes = ['serial_mixed_queries', 'serial_prefix_queries', 'overlapped_prefix_queries']
    for round_number in range(args.rounds):
        for mode in modes if round_number % 2 == 0 else reversed(modes):
            started, cpu_before = time.perf_counter(), time.process_time()
            hashes, inspections = [], []
            grouped = mode != 'serial_mixed_queries'
            overlap = mode == 'overlapped_prefix_queries'
            with ThreadPoolExecutor(2) as sources, ThreadPoolExecutor(1) as prefetch:
                pending = prefetch.submit(inspect, groups[0], grouped) if overlap else None
                for group_index, group in enumerate(groups):
                    inspections.append(pending.result() if pending else inspect(group, grouped))
                    pending = prefetch.submit(inspect, groups[group_index + 1], grouped) if overlap and group_index + 1 < len(groups) else None
                    futures = [sources.submit(copy_context().run, process, group_index * 20 + index) for index in range(len(group))]
                    hashes.extend(future.result() for future in futures)
            wall_seconds = time.perf_counter() - started
            reference = hashes if reference is None else reference
            inspection_hashes = [item['hash'] for item in inspections]
            inspection_reference = inspection_hashes if inspection_reference is None else inspection_reference
            if hashes != reference or inspection_hashes != inspection_reference:
                raise RuntimeError('Replay output mismatch')
            report = dict(mode=mode, round=round_number, images=len(samples), wall_seconds=wall_seconds,
                          cpu_seconds=time.process_time() - cpu_before,
                          inspection_seconds=sum(item['seconds'] for item in inspections),
                          logical_query_batches=sum(item['requests'] for item in inspections), outputs_identical=True)
            reports.append(report)
            print(json.dumps(report), flush=True)
            args.output.write_text(json.dumps(dict(kind='matched historical proof-read and independent model-corpus replay',
                                                   reports=reports), indent=2))


if __name__ == '__main__':
    main()
