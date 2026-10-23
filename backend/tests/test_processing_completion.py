import json

import pytest
from processing_completion import STEPS, StaleProcessingResult, check_revision, prepare_completion, relay_completion
from test_metadata_write_concurrency import _RacyTable


def accepted(intent):
    return dict(completionId=intent['id'], libraryId=intent['library'], photoId=intent['photoId'],
                processingRevision=intent['revision'])


def source():
    return dict(PartitionKey='lib', RowKey='photo.jpg', anonymousImageId='photo', processingRevision='rev-1',
        processingGeneration='a' * 64, faceCount=0, processing_metadata='{}',
        **{step + '_status': 'no_data' for step in STEPS})


def test_pending_intent_is_stable_and_exact_ack_does_not_requeue():
    row = source(); prepare_completion(row); identity = row['processingCompletionPending']
    assert identity
    prepare_completion(row); assert row['processingCompletionPending'] == identity
    row['processingCompletionAck'] = identity; prepare_completion(row)
    assert row['processingCompletionPending'] == ''
    row['processingRevision'] = 'rev-2'; prepare_completion(row)
    assert row['processingCompletionPending'] and row['processingCompletionPending'] != identity


@pytest.mark.parametrize('change', [{'face_status': 'failed'}, {'face_status': 'timeout'},
    {'processing_lease_owner': 'owner'}, {'processing_state': 'deleted'}, {'processingIsolated': True}])
def test_missing_or_unresolved_proof_never_emits_completion(change):
    row = source(); prepare_completion(row); row.update(change); prepare_completion(row)
    assert row['processingCompletionPending'] == ''


def video_source():
    """Exactly storage_utils.finalize_uploaded_file's video_status_overrides:
    thumbnail/exif land at 'failed' (browser-owned, not itself a TERMINAL
    value), the rest at 'skipped'. None of the seven are backend-required
    for video (processing_generation.required_steps_for_media(True) == ())."""
    row = source()
    row['RowKey'] = 'clip.mp4'
    row.update(preview_status='skipped', ocr_status='skipped', ai_vision_status='skipped',
               face_status='skipped', map_detection_status='skipped',
               thumbnail_status='failed', exif_status='failed')
    return row


def test_a_finalized_video_is_completion_eligible_despite_failed_thumbnail_and_exif():
    row = video_source()
    prepare_completion(row)
    assert row['processingCompletionPending']
    intent = json.loads(row['processingCompletionOutbox'])[0]
    assert intent['requiredSteps'] == []
    assert intent['stepProvenance']['thumbnail'] == 'not_required'
    assert intent['stepProvenance']['exif'] == 'not_required'
    assert intent['statuses']['thumbnail'] == 'failed'  # Reported as-is; not required to be TERMINAL.


def test_video_eligibility_is_unaffected_by_a_genuinely_stuck_required_step():
    """A video has no required backend steps at all, so even an injected
    'running'/leased step (which would block an image) doesn't block it --
    confirms eligibility truly depends on `required`, not on every step."""
    row = video_source()
    row['ocr_status'] = 'running'  # Not required for video; must not block.
    prepare_completion(row)
    assert row['processingCompletionPending']


def test_lost_notification_remains_recoverable_and_duplicate_delivery_is_safe():
    table = _RacyTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    delivered = []
    def lost_reply(row):
        delivered.append(row['processingCompletionPending']); raise RuntimeError('lost response')
    with pytest.raises(RuntimeError): relay_completion(table, 'lib', 'photo.jpg', lost_reply)
    assert table.get_entity('lib', 'photo.jpg')['processingCompletionPending']
    assert relay_completion(table, 'lib', 'photo.jpg', lambda row: (delivered.append(row['processingCompletionPending']), accepted(row))[1])
    assert len(set(delivered)) == 1
    assert not relay_completion(table, 'lib', 'photo.jpg', lambda row: pytest.fail('already acked'))


def test_old_ack_cannot_consume_a_new_completion_committed_during_delivery():
    table = _RacyTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    def replace(intent):
        fresh = table.get_entity('lib', 'photo.jpg'); fresh['processingRevision'] = 'rev-2'
        prepare_completion(fresh); table.upsert_entity(fresh)
        return accepted(intent)
    assert relay_completion(table, 'lib', 'photo.jpg', replace)
    assert table.get_entity('lib', 'photo.jpg')['processingCompletionPending']


def test_revision_fails_closed_before_result_side_effects():
    check_revision(source(), 'rev-1')
    with pytest.raises(StaleProcessingResult): check_revision(source(), 'rev-2')
    with pytest.raises(StaleProcessingResult): check_revision(source(), '')


def test_replaced_generation_keeps_its_captured_intent_until_acknowledged():
    import json
    table = _RacyTable(); row = source(); prepare_completion(row)
    old_id = row['processingCompletionPending']
    row['processingRevision'] = 'rev-2'; prepare_completion(row); table.upsert_entity(row)
    intents = []
    assert relay_completion(table, 'lib', 'photo.jpg', lambda intent: (intents.append(intent), accepted(intent))[1])
    assert intents[0]['revision'] == 'rev-1'
    assert intents[0]['id'] == old_id
    remaining = json.loads(table.get_entity('lib', 'photo.jpg')['processingCompletionOutbox'])
    assert len(remaining) == 1 and remaining[0]['revision'] == 'rev-2'


def test_outbox_capacity_blocks_commit_instead_of_evicting_unacknowledged_generations():
    row = source()
    for number in range(16):
        row['processingRevision'] = str(number); prepare_completion(row)
    row['processingRevision'] = 'overflow'
    with pytest.raises(RuntimeError, match='requires recovery'):
        prepare_completion(row)


def test_durable_inbox_accepts_before_admission_and_deduplicates_delivery():
    from upload_waves import ShadowWaves
    from test_upload_waves import AtomicTable
    import json
    row = source(); prepare_completion(row)
    intent = json.loads(row['processingCompletionOutbox'])[0]
    inbox = ShadowWaves(AtomicTable(), 'lib')
    received = inbox.accept_source_completion(intent)
    assert inbox.accept_source_completion(intent) == received
    assert received['completionQualified'] is False
    assert inbox._get('control') is None  # No stage/admission state manufactured.


@pytest.mark.parametrize('change', [{'library': 'another-library'}, {'revision': 'replacement'},
                                    {'photoId': 'replacement'}, {'statuses': {}}])
def test_inbox_rejects_rebound_or_cross_library_completion(change):
    from upload_waves import ShadowWaves
    from test_upload_waves import AtomicTable
    import json
    row = source(); prepare_completion(row)
    intent = json.loads(row['processingCompletionOutbox'])[0]; intent.update(change)
    table = AtomicTable()
    with pytest.raises(ValueError): ShadowWaves(table, 'lib').accept_source_completion(intent)
    assert not table.rows


def test_crash_after_inbox_commit_replays_same_evidence_before_source_ack():
    from upload_waves import ShadowWaves
    from test_upload_waves import AtomicTable
    inbox_table = AtomicTable(); inbox = ShadowWaves(inbox_table, 'lib')
    table = _RacyTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    def crash_after_commit(intent):
        inbox.accept_source_completion(intent); raise OSError('reply lost after commit')
    with pytest.raises(OSError): relay_completion(table, 'lib', 'photo.jpg', crash_after_commit)
    assert table.get_entity('lib', 'photo.jpg')['processingCompletionPending']
    assert relay_completion(table, 'lib', 'photo.jpg', inbox.accept_source_completion)
    assert len(inbox_table.rows) == 1


def test_result_commit_delivers_to_durable_inbox_and_retains_evidence_on_outage(monkeypatch):
    import storage_utils
    from test_upload_waves import AtomicTable
    from upload_waves import ShadowWaves
    table = _RacyTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    monkeypatch.setenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', 'true')
    monkeypatch.setitem(storage_utils._CTX, 'metadata_table_client', table)
    def unavailable(*_): raise OSError('inbox down')
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', unavailable)
    assert not storage_utils.publish_source_completion_intent('lib', 'photo.jpg', row)
    assert table.get_entity('lib', 'photo.jpg')['processingCompletionPending']
    inbox = ShadowWaves(AtomicTable(), 'lib')
    monkeypatch.setitem(storage_utils._CTX, 'source_completion_delivery', lambda library, intent: inbox.accept_source_completion(intent))
    assert storage_utils.publish_source_completion_intent('lib', 'photo.jpg', row)
    assert not table.get_entity('lib', 'photo.jpg')['processingCompletionPending']


@pytest.mark.parametrize('receipt', [None, False, {}, {'completionId': 'unrelated'}])
def test_missing_or_wrong_inbox_acceptance_never_acknowledges_source(receipt):
    table = _RacyTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    assert not relay_completion(table, 'lib', 'photo.jpg', lambda intent: receipt)
    assert table.get_entity('lib', 'photo.jpg')['processingCompletionPending']
