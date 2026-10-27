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


class _CommitResponseTable(_RacyTable):
    """Preserves MERGE fields and returns the service's post-write ETag."""
    def update_entity(self, entity, mode=None, *, etag=None, match_condition=None):
        from azure.core.exceptions import ResourceModifiedError
        from azure.data.tables import UpdateMode
        key = entity['PartitionKey'], entity['RowKey']
        version = etag if etag is not None else entity.metadata['etag']
        with self.lock:
            self.update_calls += 1
            if match_condition is not None and str(version) != str(self.etags[key]):
                raise ResourceModifiedError('changed')
            if mode == UpdateMode.MERGE:
                self.rows[key].update(dict(entity))
            else:
                self.rows[key] = dict(entity)
            self.etags[key] += 1
            return {'etag': str(self.etags[key])}


def test_committed_completion_ack_skips_both_reads_and_preserves_metadata():
    import processing_metrics
    table = _CommitResponseTable(); row = source(); row['rating'] = 3
    prepare_completion(row); table.upsert_entity(row)
    with processing_metrics.accounting() as counts:
        assert relay_completion(table, 'lib', 'photo.jpg', accepted,
                                committed=row, committed_etag='1')
    assert table.get_calls == 0 and table.update_calls == 1
    stored = table.rows['lib', 'photo.jpg']
    assert stored['rating'] == 3 and stored['processingRevision'] == 'rev-1'
    assert stored['processingCompletionOutbox'] == '[]' and not stored['processingCompletionPending']
    assert row['processingCompletionPending']  # Caller snapshot is not mutated.
    assert counts.snapshot()['completion_ack_snapshot_uses'] == 1


@pytest.mark.parametrize('replacement', [False, True])
def test_committed_ack_conflict_rereads_and_preserves_new_generation_or_edit(replacement):
    import processing_metrics
    table = _CommitResponseTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    def racing_delivery(intent):
        fresh = dict(table.rows['lib', 'photo.jpg'], rating=7)
        if replacement:
            fresh['processingRevision'] = 'rev-2'; prepare_completion(fresh)
        table.upsert_entity(fresh)
        return accepted(intent)
    with processing_metrics.accounting() as counts:
        assert relay_completion(table, 'lib', 'photo.jpg', racing_delivery,
                                committed=row, committed_etag='1')
    assert table.get_calls == 1  # One conflict recovery read; no optimistic pre-read.
    stored = table.rows['lib', 'photo.jpg']
    assert stored['rating'] == 7
    if replacement:
        assert stored['processingRevision'] == 'rev-2'
        assert stored['processingCompletionPending'] != row['processingCompletionPending']
        assert len(json.loads(stored['processingCompletionOutbox'])) == 1
    else:
        assert not stored['processingCompletionPending']
    assert counts.snapshot()['completion_ack_snapshot_conflicts'] == 1


@pytest.mark.parametrize('etag', [None, '', '*', 1])
def test_missing_genuine_commit_etag_uses_fresh_reads(etag):
    table = _CommitResponseTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    assert relay_completion(table, 'lib', 'photo.jpg', accepted, committed=row, committed_etag=etag)
    assert table.get_calls == 2


def test_committed_ack_requires_durable_acceptance_and_lost_reply_is_recoverable():
    table = _CommitResponseTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    assert not relay_completion(table, 'lib', 'photo.jpg', lambda _: {}, committed=row, committed_etag='1')
    def lost(_): raise OSError('lost response')
    with pytest.raises(OSError):
        relay_completion(table, 'lib', 'photo.jpg', lost, committed=row, committed_etag='1')
    assert table.update_calls == 0
    assert table.rows['lib', 'photo.jpg']['processingCompletionPending']
    assert relay_completion(table, 'lib', 'photo.jpg', accepted, committed=row, committed_etag='1')


def test_committed_ack_rejects_cross_source_snapshot():
    table = _CommitResponseTable(); row = source(); prepare_completion(row)
    with pytest.raises(ValueError, match='scope mismatch'):
        relay_completion(table, 'other', 'photo.jpg', lambda _: pytest.fail('delivered wrong source'),
                         committed=row, committed_etag='1')
    assert table.get_calls == table.update_calls == 0


def test_new_completion_inbox_skips_absence_read_and_retries_verify_existing(monkeypatch):
    from upload_waves import ShadowWaves
    from test_upload_waves import AtomicTable
    import processing_metrics
    table = AtomicTable(); inbox = ShadowWaves(table, 'lib')
    row = source(); prepare_completion(row); intent = json.loads(row['processingCompletionOutbox'])[0]
    reads = []; original = table.get_entity
    monkeypatch.setattr(table, 'get_entity', lambda partition, key: (reads.append(key), original(partition, key))[1])
    with processing_metrics.accounting() as metrics:
        first = inbox.accept_source_completion(intent)
        assert reads == ['policy']  # Create-only common path: no source-completion GET.
        assert inbox.accept_source_completion(intent) == first
    assert sum(k.startswith('source-completion!') for k in reads) == 1
    assert metrics.snapshot()['completion_inbox_creates'] == 1
    assert metrics.snapshot()['completion_inbox_create_conflicts'] == 1
    key = next(k for k in table.rows if k[1].startswith('source-completion!'))
    table.rows[key]['payload'] = '{}'
    from upload_waves import Conflict
    with pytest.raises(Conflict, match='rebound'): inbox.accept_source_completion(intent)


def test_cleanup_allows_existing_inbox_ack_but_fences_new_reference():
    from upload_waves import ShadowWaves, Conflict
    from test_upload_waves import AtomicTable
    table = AtomicTable(); inbox = ShadowWaves(table, 'lib')
    row = source(); prepare_completion(row); intent = json.loads(row['processingCompletionOutbox'])[0]
    first = inbox.accept_source_completion(intent)
    table.submit_transaction([('create', inbox._row('policy', cleanupState='active', gcUntil=0))])
    assert inbox.accept_source_completion(intent) == first
    row['processingRevision'] = 'rev-2'; prepare_completion(row)
    new = json.loads(row['processingCompletionOutbox'])[-1]
    with pytest.raises(Conflict, match='cleanup'): inbox.accept_source_completion(new)
    assert len([k for k in table.rows if k[1].startswith('source-completion!')]) == 1


def test_cleanup_starting_during_create_prevents_new_inbox_reference():
    from upload_waves import ShadowWaves, Conflict
    from test_upload_waves import AtomicTable
    table = AtomicTable(); inbox = ShadowWaves(table, 'lib')
    table.submit_transaction([('create', inbox._row('policy', cleanupState='', gcUntil=0))])
    row = source(); prepare_completion(row); intent = json.loads(row['processingCompletionOutbox'])[0]
    def cleanup():
        old = inbox._required('policy')
        table.submit_transaction([inbox._change(old, dict(old, cleanupState='active'))])
    table.before_commit = cleanup
    with pytest.raises(Conflict, match='cleanup'): inbox.accept_source_completion(intent)
    assert not any(k[1].startswith('source-completion!') for k in table.rows)


def test_snapshot_relay_recovers_lost_inbox_reply_without_recreating_or_losing_intent():
    from upload_waves import ShadowWaves
    from test_upload_waves import AtomicTable
    table = _CommitResponseTable(); row = source(); prepare_completion(row); table.upsert_entity(row)
    inbox_table = AtomicTable(); inbox = ShadowWaves(inbox_table, 'lib')
    def lost(intent):
        inbox.accept_source_completion(intent)
        raise OSError('reply lost after durable inbox commit')
    with pytest.raises(OSError):
        relay_completion(table, 'lib', 'photo.jpg', lost, committed=row, committed_etag='1')
    assert table.rows['lib', 'photo.jpg']['processingCompletionPending']
    assert relay_completion(table, 'lib', 'photo.jpg', inbox.accept_source_completion,
                            committed=row, committed_etag='1')
    assert table.get_calls == 0 and len(inbox_table.rows) == 1
    assert table.rows['lib', 'photo.jpg']['processingCompletionOutbox'] == '[]'
