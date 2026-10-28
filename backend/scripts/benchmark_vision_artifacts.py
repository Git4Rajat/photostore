"""Isolated artifact-promotion experiment; never imported by production workers.

Uses real JPEG decoding/hash work and a disposable local filesystem blob double.
Optional per-call delay is a sensitivity model, NOT measured Azure latency.
Inference preview GET is included in every mode for an equal accounting boundary.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from azure.core.exceptions import ResourceExistsError, ServiceResponseError
from processing_artifacts import (_read, promote_prepared, published_name,
                                  validate_derivative_bytes, verify_receipt)
from processing_completion import StaleProcessingResult
from processing_fence import (ResultFence, check_immutable_source, current_fence,
                              immutable_artifact_checks, remember_published_derivative,
                              result_fence)


class LocalStorage:
    """Small blob API double; counters include failed upload attempts."""
    def __init__(self, root, delay_ms=0):
        self.root, self.delay = Path(root), delay_ms / 1000
        self._CTX = {'blob_thumbnail_container': 'thumbs'}
        self.counts = dict(gets=0, puts=0, get_bytes=0, put_bytes=0)
        self.lock = threading.Lock()
        self.fault = None
        self.after_upload = None

    def count(self, operation, size=0):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.counts[operation] += 1
            self.counts['get_bytes' if operation == 'gets' else 'put_bytes'] += size

    def _get_blob_client(self, container, name):
        return LocalBlob(self, name)


class LocalBlob:
    def __init__(self, storage, name):
        self.storage, self.name = storage, name
        self.path = storage.root / hashlib.sha256(name.encode()).hexdigest()

    def upload_blob(self, data, *, overwrite, content_settings, validate_content=False):
        assert not overwrite and content_settings.content_type == 'image/jpeg'
        self.storage.count('puts', len(data))
        if self.storage.fault == 'interrupted':
            self.storage.fault = None
            raise ServiceResponseError('Upload interrupted before server commit')
        try:
            with self.path.open('xb') as stream:
                stream.write(data)
        except FileExistsError:
            raise ResourceExistsError('exists')
        # Exercise the extra checksum CPU; this is not an Azure transport test.
        if validate_content:
            hashlib.md5(data).digest()
        if self.storage.after_upload:
            self.storage.after_upload()
        if self.storage.fault == 'uncertain':
            self.storage.fault = None
            raise ServiceResponseError('Server committed; response was lost')
        if self.storage.fault == 'missing_etag':
            return {}
        return {'etag': self.properties(data).etag}

    def properties(self, data):
        return SimpleNamespace(size=len(data), etag='"'+hashlib.sha256(data).hexdigest()+'"',
            content_settings=SimpleNamespace(content_type='image/jpeg'))

    def download_blob(self, *, offset, length, etag=None, match_condition=None):
        data = self.path.read_bytes()
        props = self.properties(data)
        if etag is not None and etag != props.etag:
            from azure.core.exceptions import ResourceModifiedError
            raise ResourceModifiedError('changed')
        body = data[offset:offset+length]
        self.storage.count('gets', len(body))
        return SimpleNamespace(properties=props, readall=lambda: body)


class Source:
    def __init__(self, row): self.row = row
    def get_entity(self, *args): return dict(self.row)


def identity(row):
    return tuple(row.get(k) for k in ('PartitionKey', 'RowKey', 'anonymousImageId',
                                     'processingRevision', 'fileHash'))


@dataclass(frozen=True)
class Preview:
    binding: tuple
    data: bytes


def experimental_promote(storage, row, preview, *, upload_receipt=False):
    """Prototype only: retain production source checks and receipt shape."""
    from azure.storage.blob import ContentSettings
    fence = current_fence(row['PartitionKey'], row['RowKey'])
    if preview.binding != identity(row):
        raise StaleProcessingResult('Prefetched preview identity mismatch')
    for kind in ('thumbnail', 'preview'):
        if kind == 'preview':
            data = preview.data
        else:
            data, _ = _read(storage._get_blob_client('thumbs', fence.photo), kind)
        width, height = validate_derivative_bytes(data, kind)
        sha = hashlib.sha256(data).hexdigest()
        name = published_name(fence.library, fence.photo, fence.revision, kind, sha)
        blob = storage._get_blob_client('thumbs', name)
        response = None
        try:
            response = blob.upload_blob(data, overwrite=False,
                content_settings=ContentSettings(content_type='image/jpeg'),
                validate_content=upload_receipt)
        except ResourceExistsError:
            pass  # Existing objects must be read and compared.
        except ServiceResponseError:
            if not upload_receipt:
                raise
            # No receipt on ambiguous failure. Let the outer attempt retry;
            # its create conflict will force full read-back verification.
            raise
        if not upload_receipt or not response or not response.get('etag'):
            actual, props = _read(blob, kind)
            if actual != data:
                raise ValueError('Immutable derivative content mismatch')
            etag = str(props.etag)
        else:
            etag = str(response['etag'])
        check_immutable_source(fence)
        receipt = dict(schemaVersion=1, library=fence.library, photoId=fence.photo,
            revision=fence.revision, attempt=fence.attempt, kind=kind, blob=name,
            sha256=sha, size=len(data), width=width, height=height, etag=etag)
        row[kind+'Artifact'] = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
        # Benchmark-only use: production helper currently documents read-back.
        remember_published_derivative(receipt)


def run_image(storage, pair, index, mode):
    thumb, preview = pair
    row = dict(PartitionKey='benchmark', RowKey=f'{index}.jpg', anonymousImageId=str(index),
        processingRevision='revision', fileHash=hashlib.sha256(preview).hexdigest(),
        processingFence='attempt', processing_lease_owner='worker',
        processing_lease_expires_at='2099-01-01T00:00:00+00:00',
        thumbnail_status='done', preview_status='done')
    for name, data in ((str(index), thumb), (f'preview/{index}.jpg', preview)):
        storage._get_blob_client('thumbs', name).path.write_bytes(data)
    source = Source(row)
    fence = ResultFence(source, 'benchmark', row['RowKey'], 'revision', 'attempt', 'worker', str(index))
    with result_fence(fence):
        # All modes include the same inference-input fetch/validation.
        data, _ = _read(storage._get_blob_client('thumbs', f'preview/{index}.jpg'), 'preview')
        cached = Preview(identity(row), data)
        def promote():
            with immutable_artifact_checks(fence):
                if mode == 'current': promote_prepared(storage, row)
                else: experimental_promote(storage, row, cached, upload_receipt=mode=='upload_response')
        promote()
        for kind in ('thumbnail', 'preview'):
            verify_receipt(storage, row, kind, reuse_published=True)
        receipts = [json.loads(row[k+'Artifact']) for k in ('thumbnail', 'preview')]
    # Ignore receipt ETag/attempt; independently validate bytes after timing runs.
    return receipts


def corpus(directory, count):
    paths = sorted(p for p in directory.rglob('*') if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
    pairs = []
    for path in paths:
        try:
            with Image.open(path) as image:
                image = image.convert('RGB')
                values = []
                for kind, edge in (('thumbnail', 120), ('preview', 2048)):
                    scaled = image.copy(); scaled.thumbnail((edge, edge))
                    out = io.BytesIO(); scaled.save(out, 'JPEG', quality=85)
                    data = out.getvalue(); validate_derivative_bytes(data, kind); values.append(data)
                pairs.append(tuple(values))
        except (OSError, ValueError): continue
        if len(pairs) == count: break
    if not pairs: raise ValueError('No valid input images')
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--images', type=int, default=24)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--delay-ms', type=float, nargs='+', default=[0, 10])
    args = parser.parse_args()
    pairs = corpus(args.corpus, args.images)
    reports = []
    for delay in args.delay_ms:
        for round_number in range(args.rounds):
            modes = ['current', 'reuse_preview', 'upload_response']
            for mode in modes if round_number%2==0 else reversed(modes):
                with tempfile.TemporaryDirectory(prefix='vision-artifacts-') as root:
                    storage = LocalStorage(root, delay)
                    wall, cpu = time.perf_counter(), time.process_time()
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(lambda item:run_image(storage,item[1],item[0],mode),enumerate(pairs)))
                    elapsed, cpu_seconds = time.perf_counter()-wall, time.process_time()-cpu
                    # Post-run verification deliberately excluded from counters/timing.
                    for receipts in results:
                        for receipt in receipts:
                            data = storage._get_blob_client('thumbs', receipt['blob']).path.read_bytes()
                            assert hashlib.sha256(data).hexdigest()==receipt['sha256']
                            assert validate_derivative_bytes(data, receipt['kind'])==(receipt['width'],receipt['height'])
                    reports.append(dict(mode=mode,round=round_number,images=len(pairs),workers=2,
                        simulated_call_delay_ms=delay,wall_seconds=elapsed,cpu_seconds=cpu_seconds,
                        images_per_second=len(pairs)/elapsed,**storage.counts))
                    print(json.dumps(reports[-1]),flush=True)
    summary=[]
    for delay in args.delay_ms:
        for mode in ('current','reuse_preview','upload_response'):
            rows=[r for r in reports if r['mode']==mode and r['simulated_call_delay_ms']==delay]
            summary.append(dict(mode=mode,simulated_call_delay_ms=delay,
                **{key:statistics.median(r[key] for r in rows) for key in
                   ('wall_seconds','cpu_seconds','images_per_second','gets','puts','get_bytes','put_bytes')}))
    args.output.write_text(json.dumps(dict(platform=platform.platform(),at=datetime.now(timezone.utc).isoformat(),
        scope='Local filesystem blob double; delays are simulated; no models or Azure transport',
        corpus_images=len(pairs),corpus_preview_bytes=sum(len(p[1]) for p in pairs),
        output_hashes_verified=True,reports=reports,summary=summary),indent=2)+'\n')


if __name__=='__main__': main()
