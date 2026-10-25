"""Browser-prepared bytes must become fenced immutable outputs before completion."""
import json

import pytest

import app
import storage_utils
import wave_runtime
from processing_artifacts import promote_prepared, verify_receipt
from processing_completion import StaleProcessingResult
from processing_fence import result_fence
from processing_generation import allocate_generation
from processing_receipts import ArtifactReceipts
from test_processing_artifacts import Blob, blobs, jpeg
from test_processing_fence import world
from test_processing_receipts import terminal
from test_upload_waves import AtomicTable


@pytest.mark.parametrize('needs_ocr', [True, False])
def test_real_wave_adapter_qualifies_browser_prepared_derivatives(blobs, monkeypatch, needs_ocr):
    (metadata, _, _, _, _), objects = blobs
    row = metadata.rows['lib', 'photo.jpg']
    terminal(row)
    row.update(fileHash='a' * 64, thumbnail_status='done', preview_status='done',
               face_status='skipped', processing_lease_owner='', processing_lease_expires_at='')
    row.update(allocate_generation('rev', photo_id='photo', file_hash=row['fileHash'], is_video=False))
    if needs_ocr:
        row['ocr_status'] = 'pending'
    for name in ('photo', 'preview/photo.jpg'):
        blob = Blob(); blob.data = jpeg(); blob.version = 1; objects[name] = blob
    receipts = ArtifactReceipts(AtomicTable(), 'lib')
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setenv('PROCESSING_GENERATION_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    monkeypatch.setitem(storage_utils._CTX, 'source_artifact_receipt_capture',
                        lambda library, candidate: receipts.capture(storage_utils, candidate))
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: None)
    monkeypatch.setattr(app, '_register_ipwork_processors', lambda: None)
    calls = []
    def process(library, filename, steps, **kwargs):
        calls.append(steps)
        assert steps == ['ocr']
        return {'ocr': {'hasData': False, 'text': ''}}
    monkeypatch.setattr(app, '_run_ipwork_steps', process)
    wave_runtime.process_source('lib', 'photo.jpg', 'rev', lambda: None)
    stored = metadata.get_entity('lib', 'photo.jpg')
    assert bool(calls) is needs_ocr
    assert stored['artifactReceipt'] and not stored['processing_lease_owner']
    for kind in ('thumbnail', 'preview'):
        proof = verify_receipt(storage_utils, stored, kind)
        assert proof['revision'] == 'rev' and proof['blob'].startswith('derived/v1/')
        assert objects[proof['blob']].data == jpeg()
    manifest = json.loads(receipts._get('manifest!' + json.loads(stored['artifactReceipt'])['id'])['payload'])
    assert set(manifest['derivatives']) == {'thumbnail', 'preview'}
    intent = json.loads(stored['processingCompletionOutbox'])[-1]
    assert receipts.verify_reference(json.loads(stored['artifactReceipt']), intent)['derivatives'] == manifest['derivatives']
    # Retry must keep the published content even if a still-valid browser SAS
    # subsequently overwrites the old preparation locations.
    for name in ('photo', 'preview/photo.jpg'):
        objects[name].data = jpeg('blue'); objects[name].version += 1
    wave_runtime.process_source('lib', 'photo.jpg', 'rev', lambda: None)
    retried = metadata.get_entity('lib', 'photo.jpg')
    assert len(calls) == int(needs_ocr)
    for kind in ('thumbnail', 'preview'):
        assert retried[kind + 'Artifact'] == stored[kind + 'Artifact']
        assert verify_receipt(storage_utils, retried, kind)['sha256'] == manifest['derivatives'][kind]['sha256']


@pytest.mark.parametrize('extension', ['mov', 'mp4', 'mxf'])
def test_video_completion_without_face_count_does_not_run_models_or_read_original(blobs, monkeypatch, extension):
    from execution_lease import ExecutionLeases
    from test_processing_completion import video_source
    from test_wave_recovery import RuntimeTable
    from upload_waves import ShadowWaves
    from upload_wave_execution import run_vision_release, stage_work_store, _iter_wave_faces
    from wave_policy import certify, PROTOCOL, ROLES

    (metadata, _, _, _, _), objects = blobs
    filename = 'dnxhd100_cid1260.' + extension
    row = dict(metadata.get_entity('lib', 'photo.jpg'))
    row.update(video_source(), RowKey=filename, fileHash='a' * 64,
        processing_lease_owner='', processing_lease_expires_at='')
    row.pop('faceCount')
    row.update(allocate_generation('video-rev', photo_id='photo', file_hash=row['fileHash'], is_video=True))
    metadata.upsert_entity(row)
    table = RuntimeTable(); store = ShadowWaves(table, 'lib'); receipts = ArtifactReceipts(table, 'lib')
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setenv('PROCESSING_GENERATION_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('UPLOAD_WAVE_EXECUTION_ENABLED', 'true')
    certificate = certify(table, {role: 'build' for role in ROLES})
    table.create_entity(store._row('policy', protocol=PROTOCOL, capabilityId=certificate, cleanupState=''))
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    monkeypatch.setitem(storage_utils._CTX, 'source_artifact_receipt_capture',
        lambda library, candidate: receipts.capture(storage_utils, candidate))
    monkeypatch.setattr(app, '_run_ipwork_steps', lambda *a, **k: pytest.fail('Video ran backend models'))
    monkeypatch.setattr(storage_utils, 'download_media_bytes', lambda *a, **k: pytest.fail('Video original downloaded'))
    opened = store.open_session('videos', 1)
    store.register('videos', [dict(itemId='video', filename=filename)])
    resolved = store.resolve('videos', 'video', outcome='accepted', filename=filename,
        photo_id='photo', processing_revision='video-rev', required_steps=())
    store.close('videos', resolved['manifestRevision'], 'done')
    wave_id = opened['waveId']; leases = ExecutionLeases(table, partition=store.partition)
    result = run_vision_release(leases, stage_work_store(table), store, 'lib', wave_id,
        store.status(wave_id)['visionPendingKey'], metadata, None, None, lambda *a: None,
        process_source=wave_runtime.process_source)
    assert result['state'] == 'indexing'
    stored = metadata.get_entity('lib', filename)
    assert stored['faceCount'] == 0 and stored['artifactReceipt']
    assert not stored['processing_lease_owner']
    assert list(_iter_wave_faces(store, wave_id)) == []
    assert not objects
    successor = store.open_session('photos', 1)
    store.register('photos', [dict(itemId='photo', filename='next.jpg')])
    from processing_generation import REQUIRED_STEPS
    resolved = store.resolve('photos', 'photo', outcome='accepted', filename='next.jpg',
        photo_id='next-photo', processing_revision='next-rev', required_steps=REQUIRED_STEPS)
    store.close('photos', resolved['manifestRevision'], 'done')
    assert store.status(successor['waveId'])['state'] == 'waiting_predecessor'
    from upload_wave_execution import run_index_release
    from wave_publication import ObligationLedger
    ledger = ObligationLedger(table, wave_id, library='lib', frozen_revision=result['frozenRevision'],
        assignment_revision=result['assignmentReceiptId'])
    completed = run_index_release(leases, store, 'lib', wave_id, result['indexPendingKey'], ledger,
        publisher=lambda scope, obligation, check: 'published-' + scope)
    assert completed['state'] == 'complete'
    assert store.promote_successor(successor['waveId'])['state'] == 'processing_pending'


def test_thumbnail_only_preparation_is_promoted_without_requiring_preview(blobs):
    (metadata, _, _, _, fence), objects = blobs
    row = dict(metadata.get_entity('lib', 'photo.jpg'), thumbnail_status='done', preview_status='pending')
    thumbnail = Blob(); thumbnail.data = jpeg(); thumbnail.version = 1
    objects['photo'] = thumbnail
    with result_fence(fence):
        promote_prepared(storage_utils, row)
        proof = verify_receipt(storage_utils, row, 'thumbnail')
    assert objects[proof['blob']].data == jpeg()
    assert row['thumbnail_status'] == 'done'
    assert row['preview_status'] == 'pending'
    assert not row.get('previewArtifact')
    assert 'preview/photo.jpg' not in objects


@pytest.mark.parametrize('bad_bytes', [b'not-a-jpeg', jpeg(edge=121)])
def test_preparation_done_does_not_bypass_byte_validation(blobs, bad_bytes):
    (metadata, _, _, _, fence), objects = blobs
    row = dict(metadata.get_entity('lib', 'photo.jpg'), thumbnail_status='done')
    blob = Blob(); blob.data = bad_bytes; blob.version = 1; objects['photo'] = blob
    with result_fence(fence), pytest.raises((ValueError, OSError)):
        promote_prepared(storage_utils, row)
    assert not row.get('thumbnailArtifact')
    assert not any(name.startswith('derived/') for name in objects)


def test_source_change_during_preparation_read_cannot_publish(blobs):
    (metadata, _, _, _, fence), objects = blobs
    row = dict(metadata.get_entity('lib', 'photo.jpg'), thumbnail_status='done')
    blob = Blob(); blob.data = jpeg(); blob.version = 1; objects['photo'] = blob
    def replaced():
        current = metadata.get_entity('lib', 'photo.jpg')
        metadata.upsert_entity(dict(current, processingFence='successor'))
    blob.before_download = replaced
    with result_fence(fence), pytest.raises(StaleProcessingResult):
        promote_prepared(storage_utils, row)
    assert not row.get('thumbnailArtifact')
    assert not any(name.startswith('derived/') for name in objects)
