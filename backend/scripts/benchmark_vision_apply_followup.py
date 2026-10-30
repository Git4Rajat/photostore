"""Isolated face persistence/capture benchmark against deployed receipt reuse.

No model, semantic refresh, blob promotion or full-wave throughput is measured.
Parallel capture is an experiment: it reads mutable face/embedding rows freshly
within each capture, retains the final lookup reread and all production checks.
"""
import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, wait
import json
import os
from pathlib import Path
import benchmark_vision_persistence as base
from processing_receipt_cache import committed_receipt_scope


class SnapshotTable:
    def __init__(self, table, rows):
        self.table, self.rows = table, dict(rows)
    def get_entity(self, partition_key, row_key):
        key = partition_key, row_key
        if key in self.rows:
            return self.rows.pop(key)
        return self.table.get_entity(partition_key, row_key)
    def __getattr__(self, name):
        return getattr(self.table, name)


class StorageView:
    def __init__(self, storage, context):
        self.storage, self._CTX = storage, context
    def __getattr__(self, name):
        return getattr(self.storage, name)


@contextmanager
def parallel_capture(enabled):
    if not enabled:
        yield
        return
    original = base.ArtifactReceipts.capture
    with ThreadPoolExecutor(4) as readers:
        def capture(self, storage, metadata):
            # Four-plus faces keep the existing consolidated query path.
            if not 0 < metadata.get('faceCount', 0) < 4:
                return original(self, storage, metadata)
            fence = base.fencing.current_fence(self.library, metadata['RowKey'])
            if fence is None:
                return original(self, storage, metadata)
            base.fencing.check_immutable_source(fence)
            context = dict(storage._CTX)
            lookup = context['face_by_filename_table_client']
            row = lookup.get_entity(self.library, fence.filename)
            ids = storage._validated_face_filename_ids(row)
            if ids is None or len(ids) != metadata['faceCount']:
                return original(self, storage, metadata)
            tasks = [(name, fid) for name in ('face_table_client', 'face_embeddings_table_client')
                     if context.get(name) is not None for fid in sorted(ids)]
            futures = [readers.submit(context[name].get_entity, self.library, fid) for name, fid in tasks]
            # Drain every started read on success or failure before scope exits.
            wait(futures)
            rows = {}
            for (name, fid), future in zip(tasks, futures):
                rows.setdefault(name, {})[self.library, fid] = future.result()
            for name, values in rows.items():
                context[name] = SnapshotTable(context[name], values)
            context['face_by_filename_table_client'] = SnapshotTable(lookup, {(self.library, fence.filename): row})
            return original(self, StorageView(storage, context), metadata)
        with base.patch(base.ArtifactReceipts, 'capture', capture):
            yield


def run(tables, *, mode, **kwargs):
    with base.patch(base, 'receipt_scope', committed_receipt_scope), parallel_capture('capture' in mode):
        return base.execute(tables, mode='batch_parallel' if 'batch' in mode else 'current', **kwargs) | {'mode': mode}


def main():
    from azure.data.tables import TableServiceClient
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--images', type=int, default=32)
    parser.add_argument('--rounds', type=int, default=4)
    args = parser.parse_args()
    service = TableServiceClient.from_connection_string(os.environ['BENCHMARK_STORAGE_CONNECTION'])
    tables = {n: service.get_table_client('bench'+n) for n in ('metadata','faces','embeddings','lookup','jobs')}
    reports, references = [], {}
    base.storage._LOGGER.setLevel('ERROR')
    for faces, seeded in [(0,False),(1,False),(2,False),(8,False),(1,True)]:
        for round_number in range(args.rounds):
            modes = ['production', 'capture_parallel', 'batch_parallel', 'batch_capture_parallel']
            for mode in modes if round_number % 2 == 0 else reversed(modes):
                row = run(tables, mode=mode, face_count=faces, seeded=seeded, images=args.images, group=8)
                references.setdefault((faces,seeded),row['output_digest'])
                assert references[faces,seeded] == row['output_digest'], 'Output mismatch'
                row['round'] = round_number
                reports.append(row)
                args.output.write_text(json.dumps(dict(scope=__doc__, baseline='production committed_receipt_scope', output_parity=True, reports=reports),indent=2))
                print('ROUND '+json.dumps({k:row[k] for k in ('mode','faces','seeded','round','wall_seconds','requests')}),flush=True)


if __name__ == '__main__':
    main()
