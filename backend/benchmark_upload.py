"""Opt-in dev upload benchmarks; isolation is durable, not worker downtime."""
import io
import json
import os
import re

from PIL import Image
from azure.core import MatchConditions


def isolated(entity):
    return bool(entity and entity.get('processingIsolated') is True)


def authorize_entry(app, entry, filename):
    benchmark = entry.get('benchmark')
    if benchmark is None and not str(filename or '').startswith('rawbench-'):
        return {}
    allowed = os.getenv('BENCHMARK_UPLOAD_EMAIL', '').strip().lower()
    payload, error = app._resolve_session_payload(True)
    if error or not allowed or not payload or str(payload.get('email') or '').lower() != allowed:
        raise PermissionError('Upload benchmark is not enabled for this account')
    if not isinstance(benchmark, dict):
        raise ValueError('Benchmark reservation is required')
    run_id = str(benchmark.get('runId') or '')
    arm = str(benchmark.get('arm') or '')
    if not re.fullmatch(r'[a-z0-9-]{8,64}', run_id) or arm not in {'originals', 'derivatives'}:
        raise ValueError('Invalid benchmark run or arm')
    if not re.fullmatch(rf'rawbench-{re.escape(run_id)}-{arm}-\d{{5}}\.[A-Za-z0-9]+', str(filename)):
        raise ValueError('Filename is outside the benchmark namespace')
    if not re.fullmatch(r'[a-f0-9]{64}', str(entry.get('sha256') or '')):
        raise ValueError('Benchmark requires the original SHA-256')
    return {'processingIsolated': True, 'benchmarkRunId': run_id, 'benchmarkArm': arm}


def validate_finalization(app, library_id, entry, filename):
    fields = authorize_entry(app, entry, filename)
    if not fields:
        return
    row = app.metadata_table_client.get_entity(partition_key=library_id, row_key=filename)
    physical = row.get('pendingAnonymousBlob') or row.get('anonymousImageId')
    if (not isolated(row) or row.get('benchmarkRunId') != fields['benchmarkRunId']
            or row.get('benchmarkArm') != fields['benchmarkArm']
            or physical != entry.get('blobName')
            or row.get('upload_sha256_expected') != entry.get('sha256')):
        raise ValueError('Benchmark reservation does not match finalization')


def verify_jpeg(storage, name, *, byte_cap, edge_cap):
    blob = storage._get_blob_client(storage._CTX['blob_thumbnail_container'], name)
    if blob is None:
        raise ValueError('Derivative storage is unavailable')
    properties = blob.get_blob_properties()
    size = int(getattr(properties, 'size', 0) or 0)
    if not 0 < size <= byte_cap:
        raise ValueError('Direct derivative exceeds byte limit or is empty')
    content_type = getattr(getattr(properties, 'content_settings', None), 'content_type', '')
    if content_type != 'image/jpeg':
        raise ValueError('Direct derivative must have image/jpeg content type')
    # Read only the bounded JPEG, never the original RAW or an embedded preview.
    data = blob.download_blob(offset=0, length=size, etag=properties.etag,
                              match_condition=MatchConditions.IfNotModified).readall()
    if len(data) != size:
        raise ValueError('Direct derivative changed during verification')
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'JPEG' or max(image.size) > edge_cap:
            raise ValueError('Direct derivative exceeds pixel limit or is not JPEG')
        image.verify()
    return size


def apply_isolated_derivatives(storage, metadata, payload, report):
    physical = metadata.get('anonymousImageId')
    if not physical:
        raise ValueError('Isolated upload has no physical blob identity')
    processing = json.loads(metadata.get('processing_metadata') or '{}')
    preview = payload.get('preview')
    if isinstance(preview, dict) and preview.get('alreadyUploaded') is True:
        size = verify_jpeg(storage, f'preview/{physical}.jpg', byte_cap=1_500_000, edge_cap=2048)
        metadata['preview_status'] = 'done'
        processing['client_preview'] = {
            'source': 'browser', 'sourceKind': 'raw_converted_jpeg', 'bytes': size,
            'sourceFormat': preview.get('sourceFormat'), 'rawParserVersion': preview.get('rawParserVersion'),
            'previewWidth': preview.get('previewWidth'), 'previewHeight': preview.get('previewHeight'),
        }
    if any(item.get('step') == 'thumbnail' and item.get('status') == 'done' for item in report):
        size = verify_jpeg(storage, physical, byte_cap=512_000, edge_cap=120)
        metadata['thumbnail_status'] = 'done'
        processing['client_thumbnail'] = {'source': 'browser', 'sourceKind': 'raw_converted_jpeg', 'bytes': size}
    metadata['processing_metadata'] = json.dumps(processing)
