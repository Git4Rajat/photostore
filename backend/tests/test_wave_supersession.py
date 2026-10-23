from processing_generation import allocate_generation, supersede
from wave_supersession import member_outcome, settlement_requires_original_output


def row(revision='rev-1', **overrides):
    base = dict(RowKey='photo.jpg', anonymousImageId='photo-1', fileHash='a' * 64)
    base.update(allocate_generation(revision, photo_id=base['anonymousImageId'],
                                    file_hash=base['fileHash'], is_video=False))
    base.update(overrides)
    return base


def member(revision='rev-1'):
    return dict(photoId='photo-1', processingRevision=revision)


def test_valid_when_row_still_matches_the_frozen_member():
    assert member_outcome(member(), row(), is_video=False) == 'valid'
    assert settlement_requires_original_output('valid') is True


def test_missing_when_the_row_no_longer_exists():
    assert member_outcome(member(), None, is_video=False) == 'missing'


def test_superseded_after_a_forced_reprocess_changes_the_revision():
    current = row()
    fields = supersede(current, 'rev-2', is_video=False)
    current.update(fields)
    assert member_outcome(member(), current, is_video=False) == 'superseded'
    assert settlement_requires_original_output('superseded') is False


def test_superseded_once_the_live_model_contract_moves_on(monkeypatch):
    current = row()
    monkeypatch.setenv('FACE_CLUSTER_EMBEDDING_VERSION', 'a-brand-new-model-v2')
    assert member_outcome(member(), current, is_video=False) == 'superseded'


def test_superseded_never_silently_adopts_the_new_generations_identity():
    """The frozen member's own revision stays what it is -- supersession is
    reported, not papered over by quietly updating the member's capture."""
    original_member = member()
    current = row()
    current.update(supersede(current, 'rev-2', is_video=False))
    outcome = member_outcome(original_member, current, is_video=False)
    assert outcome == 'superseded'
    assert original_member['processingRevision'] == 'rev-1'  # Untouched.
