import json

import pytest

import storage_utils
from azure.core.exceptions import ResourceNotFoundError

from processing_completion import STEPS, prepare_completion
from processing_fence import result_fence
from processing_receipts import ArtifactReceipts
from upload_waves import ShadowWaves
from test_processing_artifacts import blobs
from test_processing_fence import world
from test_face_by_filename_lookup import _face
from test_upload_waves import AtomicTable


def terminal(metadata):
    metadata.update({step + '_status': 'no_data' for step in STEPS})
    metadata['faceCount'] = 0
    return metadata


def test_immutable_face_snapshot_survives_live_face_changes_and_qualifies_inbox(blobs):
    (metadata, faces, embeddings, lookup, fence), _ = blobs
    jobs = AtomicTable(); receipts = ArtifactReceipts(jobs, 'lib')
    row = terminal(dict(metadata.get_entity('lib', 'photo.jpg')))
    with result_fence(fence):
        storage_utils._store_client_face_entities('lib', 'photo.jpg', [_face(0) | {'embeddingVersion': 'v1'}])
        row['face_status'] = 'done'; row['faceCount'] = 1
        reference = receipts.capture(storage_utils, row)
    row['artifactReceipt'] = json.dumps(reference)
    row.update(processing_lease_owner='', processing_lease_expires_at='')
    prepare_completion(row); intent = json.loads(row['processingCompletionOutbox'])[0]
    face_id = json.loads(lookup.get_entity('lib', 'photo.jpg')['faceIds'])[0]
    embeddings.rows['lib', face_id]['embedding'] = '[9,9,9]'
    snapshot = receipts.verify_reference(reference, intent)
    source = receipts._get('face!' + snapshot['faceSnapshots'][0])
    assert json.loads(json.loads(source['payload'])['embedding']) == [.1, .2, .3]
    received = ShadowWaves(jobs, 'lib').accept_source_completion(intent)
    assert received['artifactReceiptQualified'] is True
    assert received['completionQualified'] is False  # Scheduling/model-policy is separate.


def test_manifest_is_not_published_after_a_partial_snapshot_failure(blobs, monkeypatch):
    (metadata, _, _, _, fence), _ = blobs
    table = AtomicTable(); receipts = ArtifactReceipts(table, 'lib')
    row = terminal(dict(metadata.get_entity('lib', 'photo.jpg')))
    with result_fence(fence):
        storage_utils._store_client_face_entities('lib', 'photo.jpg', [
            _face(0) | {'embeddingVersion': 'v1'}, _face(20) | {'embeddingVersion': 'v1'}])
        row.update(face_status='done', faceCount=2)
        submit = table.submit_transaction
        failed = [False]
        def outage(ops):
            if ops[0][1]['RowKey'].startswith('face!') and len(table.rows) == 1 and not failed[0]:
                failed[0] = True; raise OSError('snapshot outage')
            return submit(ops)
        monkeypatch.setattr(table, 'submit_transaction', outage)
        with pytest.raises(OSError): receipts.capture(storage_utils, row)
        assert not any(key[1].startswith('manifest!') for key in table.rows)
        reference = receipts.capture(storage_utils, row)
    assert reference and len(table.rows) == 3


def test_empty_face_lookup_is_explicit_and_missing_lookup_cannot_qualify(blobs):
    (metadata, _, _, lookup, fence), _ = blobs
    receipts = ArtifactReceipts(AtomicTable(), 'lib')
    row = terminal(dict(metadata.get_entity('lib', 'photo.jpg')))
    with result_fence(fence):
        with pytest.raises(ResourceNotFoundError): receipts.capture(storage_utils, row)
        assert not receipts.table.rows
        storage_utils._store_client_face_entities('lib', 'photo.jpg', [])
        assert receipts.capture(storage_utils, row)


def test_receipt_storage_outage_preserves_completed_outputs_and_retries_without_models(blobs, monkeypatch):
    import base64
    from test_processing_artifacts import jpeg
    (metadata, _, _, _, fence), _ = blobs
    jobs = AtomicTable(); receipts = ArtifactReceipts(jobs, 'lib')
    row = metadata.rows['lib', 'photo.jpg']
    terminal(row); row.update(face_status='skipped', thumbnail_status='running')
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setitem(storage_utils._CTX, 'embeddings_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    monkeypatch.setattr(storage_utils, '_refresh_semantic_fields', lambda *a: None)
    def unavailable(*_): raise OSError('receipt store unavailable')
    monkeypatch.setitem(storage_utils._CTX, 'source_artifact_receipt_capture', unavailable)
    with pytest.raises(OSError):
        storage_utils.apply_client_processing_results_for_file('lib', 'photo.jpg', claimed_steps=['thumbnail'],
            lease_owner='owner', expected_processing_revision='rev', expected_processing_fence='attempt',
            client_processing={'thumbnail': {'data': base64.b64encode(jpeg()).decode()}})
    persisted = metadata.get_entity('lib', 'photo.jpg')
    assert persisted['thumbnail_status'] == 'done' and persisted['thumbnailArtifact']
    assert not persisted.get('artifactReceipt')
    monkeypatch.setitem(storage_utils._CTX, 'source_artifact_receipt_capture',
                        lambda library, meta: receipts.capture(storage_utils, meta))
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', lambda *a, **k: pytest.fail('models/results replayed'))
    lease = storage_utils.claim_processing_lease('lib', 'photo.jpg', 'recovery', mark_running=False)
    recovered = storage_utils.qualify_completed_processing('lib', 'photo.jpg', lease)
    assert recovered['artifactReceipt'] and recovered['processing_lease_owner'] == ''


def test_incomplete_aggregate_cannot_leave_done_status_without_fields(blobs, monkeypatch):
    (metadata, _, _, _, _), _ = blobs
    terminal(metadata.rows['lib', 'photo.jpg'])
    metadata.rows['lib', 'photo.jpg']['face_status'] = 'running'
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', None)
    def failed_apply(*a, **k):
        storage_utils.mark_step_done('lib', 'photo.jpg', 'face')
        raise RuntimeError('failure before aggregate fields were completed')
    monkeypatch.setattr(storage_utils, '_apply_client_processing_results', failed_apply)
    with pytest.raises(RuntimeError):
        storage_utils.apply_client_processing_results_for_file('lib', 'photo.jpg', claimed_steps=['face'],
            lease_owner='owner', expected_processing_revision='rev', expected_processing_fence='attempt')
    row = metadata.get_entity('lib', 'photo.jpg')
    assert row['face_status'] == 'failed' and not row.get('artifactReceipt')


def test_video_artifact_receipt_does_not_require_browser_owned_failed_steps(blobs):
    from dataclasses import replace
    from test_processing_completion import video_source
    (metadata, _, _, _, fence), _ = blobs
    row = dict(metadata.get_entity('lib', 'photo.jpg'))
    row.update(video_source(), processingRevision=fence.revision, anonymousImageId=fence.photo,
        processingGeneration=fence.generation)
    metadata.rows['lib', 'clip.mp4'] = dict(row,
        processing_lease_owner='owner', processingFence=fence.attempt,
        processing_lease_expires_at=metadata.get_entity('lib', 'photo.jpg')['processing_lease_expires_at'])
    receipts = ArtifactReceipts(AtomicTable(), 'lib')
    with result_fence(replace(fence, filename='clip.mp4')):
        reference = receipts.capture(storage_utils, row)
    assert reference and json.loads(receipts._get('manifest!' + reference['id'])['payload'])['faceSnapshots'] == []
