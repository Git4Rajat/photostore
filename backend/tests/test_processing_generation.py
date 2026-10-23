import pytest

from processing_generation import (
    allocate_generation, build_contract, contract_id, current_contract,
    is_superseded, required_steps_for_media, supersede,
)


def image_row(**overrides):
    row = dict(RowKey='photo.jpg', anonymousImageId='', fileHash='a' * 64)
    row.update(overrides)
    row.update(allocate_generation('rev-1', photo_id=row['anonymousImageId'] or row['RowKey'],
                                   file_hash=row['fileHash'], is_video=False))
    return row


def test_required_steps_differ_by_media_type():
    assert required_steps_for_media(False) == (
        'preview', 'thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face')
    assert required_steps_for_media(True) == ()


def test_build_contract_requires_photo_and_source_identity():
    with pytest.raises(ValueError):
        build_contract(photo_id='', file_hash='a' * 64, is_video=False)
    with pytest.raises(ValueError):
        build_contract(photo_id='photo', file_hash='', is_video=False)


def test_contract_id_is_deterministic_and_binds_photo_source_and_media_type():
    a = build_contract(photo_id='photo-1', file_hash='h1', is_video=False)
    assert contract_id(a) == contract_id(build_contract(photo_id='photo-1', file_hash='h1', is_video=False))
    assert contract_id(a) != contract_id(build_contract(photo_id='photo-2', file_hash='h1', is_video=False))
    assert contract_id(a) != contract_id(build_contract(photo_id='photo-1', file_hash='h2', is_video=False))
    assert contract_id(a) != contract_id(build_contract(photo_id='photo-1', file_hash='h1', is_video=True))


def test_allocate_generation_binds_revision_photo_and_source():
    fields = allocate_generation('rev-1', photo_id='photo-1', file_hash='h1', is_video=False)
    assert fields['processingRevision'] == 'rev-1'
    assert fields['processingGeneration'] == contract_id(build_contract(photo_id='photo-1', file_hash='h1', is_video=False))
    again = allocate_generation('rev-1', photo_id='photo-1', file_hash='h1', is_video=False)
    assert fields == again  # Deterministic for identical inputs.


def test_allocate_generation_requires_a_revision():
    with pytest.raises(ValueError):
        allocate_generation('', photo_id='photo-1', file_hash='h1', is_video=False)


def test_is_superseded_false_for_a_fresh_matching_allocation():
    row = image_row()
    assert is_superseded(row, is_video=False) is False


def test_is_superseded_true_for_a_row_with_no_generation_at_all():
    row = dict(RowKey='photo.jpg', anonymousImageId='', fileHash='a' * 64)
    assert is_superseded(row, is_video=False) is True


def test_is_superseded_true_once_the_live_model_contract_moves_on(monkeypatch):
    row = image_row()
    assert is_superseded(row, is_video=False) is False
    monkeypatch.setenv('FACE_CLUSTER_EMBEDDING_VERSION', 'a-brand-new-model-v2')
    assert is_superseded(row, is_video=False) is True


def test_is_superseded_true_when_media_type_disagrees_with_the_frozen_generation():
    # The row was allocated as an image (full required-step set); checking
    # it against the video contract (no backend-required steps) must not
    # match -- media type is part of the frozen contract.
    row = image_row()
    assert is_superseded(row, is_video=True) is True


def test_ordinary_non_anonymized_row_falls_back_to_filename_as_photo_identity():
    row = dict(RowKey='photo.jpg', fileHash='a' * 64)
    row.update(allocate_generation('rev-1', photo_id=row['RowKey'], file_hash=row['fileHash'], is_video=False))
    assert is_superseded(row, is_video=False) is False
    assert row['processingGeneration'] == contract_id(build_contract(photo_id='photo.jpg', file_hash='a' * 64, is_video=False))


def test_supersede_requires_a_distinct_fresh_revision():
    row = image_row()
    with pytest.raises(ValueError, match='distinct'):
        supersede(row, row['processingRevision'], is_video=False)
    with pytest.raises(ValueError):
        supersede(row, '', is_video=False)


def test_supersede_allocates_a_new_revision_and_resets_required_steps():
    row = image_row(preview_status='done', face_status='failed')
    fields = supersede(row, 'rev-2', is_video=False)
    assert fields['processingRevision'] == 'rev-2'
    # Same photo/source/live-contract: the generation ID (which identifies
    # the CONTRACT, not the specific attempt) is unchanged -- only a model/
    # policy bump or a different photo/source would change it.
    assert fields['processingGeneration'] == row['processingGeneration']
    assert all(fields[step + '_status'] == 'pending' for step in required_steps_for_media(False))


def test_supersede_allocates_a_new_generation_when_the_live_contract_has_moved_on(monkeypatch):
    row = image_row()
    monkeypatch.setenv('FACE_CLUSTER_EMBEDDING_VERSION', 'a-brand-new-model-v2')
    fields = supersede(row, 'rev-2', is_video=False)
    assert fields['processingGeneration'] != row['processingGeneration']


def test_supersede_resets_no_steps_for_video_since_none_are_backend_required():
    row = image_row(**{'processingRevision': None})
    row.pop('processingRevision')
    row.update(allocate_generation('rev-1', photo_id='photo.mov', file_hash='h1', is_video=True))
    fields = supersede(row, 'rev-2', is_video=True)
    assert not any(key.endswith('_status') for key in fields)


def test_current_contract_reflects_live_face_embedding_versions(monkeypatch):
    monkeypatch.setenv('FACE_CLUSTER_EMBEDDING_VERSION', 'custom-version')
    contract = current_contract()
    assert 'custom-version' in contract['faceEmbeddingVersions']
