"""Real snapshots: conservative retention and concurrent restore protection."""
import json
import os
import time
import uuid

import pytest

pytest.importorskip('faiss')
import clustering_checkpoint as checkpoint
from clustering_runtime import LiveFaceIndex


@pytest.fixture
def snapshot(tmp_path):
    runtime = LiveFaceIndex(tmp_path / 'work')
    runtime.build([dict(faceId='a', personId='alice', embedding=[1, 0],
                        tier='5pt', embeddingVersion='v1')])
    share = tmp_path / 'share'
    yield runtime, share
    runtime.close()


def age(path, seconds=90000):
    stamp = time.time() - seconds
    for file in path.iterdir():
        if not file.is_symlink():
            os.utime(file, (stamp, stamp))
    os.utime(path, (stamp, stamp))


def test_roundtrip_revision_pair_and_no_training(snapshot, tmp_path, monkeypatch):
    runtime, share = snapshot
    runtime.upsert('b', 'bob', [0, 1], '5pt', 'v1')
    runtime.save_checkpoint(share, source_revision=('external', 'assignment'))
    monkeypatch.setattr(LiveFaceIndex, 'build', lambda *a: pytest.fail('must not train'))
    restored = LiveFaceIndex.restore_checkpoint(share, tmp_path / 'restored',
                                               source_revision=('external', 'assignment'))
    assert restored is not None
    try:
        assert restored.best_two([0, 1], '5pt', 'v1') == (1, 0, 'bob')
    finally:
        restored.close()
    assert LiveFaceIndex.restore_checkpoint(share, tmp_path / 'stale',
                                           source_revision=('external', 'older')) is None


def test_retention_protects_current_recent_young_and_unknown(snapshot, tmp_path):
    runtime, share = snapshot
    generations = [runtime.save_checkpoint(share, source_revision=i) for i in range(5)]
    # Make CURRENT older than all others: it must remain independently protected.
    age(share / generations[-1], 100000)
    for number, generation in enumerate(generations[:-1]):
        age(share / generation, 95000 - number)
    young = share / ('gen-' + uuid.uuid4().hex)
    young.mkdir()
    (young / 'faces.sqlite3').write_bytes(b'partial')
    unknown = share / 'user-data'
    unknown.mkdir()
    (unknown / 'important').write_text('keep')
    foreign = share / ('gen-' + uuid.uuid4().hex)
    foreign.mkdir()
    (foreign / 'important').write_text('keep')
    age(foreign)
    linked = share / ('gen-' + uuid.uuid4().hex)
    linked.symlink_to(unknown, target_is_directory=True)
    child_link = share / ('gen-' + uuid.uuid4().hex)
    child_link.mkdir()
    (child_link / 'faces.sqlite3').symlink_to(unknown / 'important')
    age(child_link)
    removed = checkpoint.cleanup_checkpoints(share)
    assert set(removed) == set(generations[:2])
    assert all((share / gen).exists() for gen in generations[2:])
    assert young.exists() and linked.is_symlink() and child_link.exists()
    assert (foreign / 'important').read_text() == 'keep'
    assert (unknown / 'important').read_text() == 'keep'
    restored = LiveFaceIndex.restore_checkpoint(share, tmp_path / 'restore', source_revision=4)
    assert restored is not None
    restored.close()


def test_retention_reaps_only_aged_abandoned_generated_files(snapshot):
    runtime, share = snapshot
    runtime.save_checkpoint(share, source_revision=1)
    abandoned = share / ('gen-' + uuid.uuid4().hex)
    abandoned.mkdir()
    (abandoned / 'faces.sqlite3').write_bytes(b'partial')
    age(abandoned)
    assert checkpoint.cleanup_checkpoints(share) == [abandoned.name]
    assert not abandoned.exists()


@pytest.mark.parametrize('bad_current', ['missing', 'corrupt'])
def test_retention_invalid_current_never_deletes(snapshot, bad_current):
    runtime, share = snapshot
    generation = runtime.save_checkpoint(share, source_revision=1)
    age(share / generation)
    if bad_current == 'missing':
        (share / 'CURRENT').unlink()
    else:
        (share / 'CURRENT').write_text('{}')
    assert checkpoint.cleanup_checkpoints(share) == []
    assert (share / generation).exists()


def test_restore_reader_lock_prevents_cleanup_during_copy(snapshot, tmp_path, monkeypatch):
    runtime, share = snapshot
    generations = [runtime.save_checkpoint(share, source_revision=i) for i in range(4)]
    for gen in generations:
        age(share / gen)
    original = checkpoint._stream_file
    attempted = []

    def copying(source, destination=None):
        if destination is not None:
            attempted.append(checkpoint.cleanup_checkpoints(share))
            assert all((share / gen).exists() for gen in generations)
        return original(source, destination)

    monkeypatch.setattr(checkpoint, '_stream_file', copying)
    restored = LiveFaceIndex.restore_checkpoint(share, tmp_path / 'restore', source_revision=3)
    assert restored is not None and attempted and all(item == [] for item in attempted)
    restored.close()
    assert checkpoint.cleanup_checkpoints(share)


def test_cleanup_error_does_not_fail_successful_publication(snapshot, monkeypatch):
    runtime, share = snapshot
    def fail(*args):
        raise OSError('cleanup unavailable')
    monkeypatch.setattr(checkpoint, '_cleanup_checkpoints', fail)
    generation = runtime.save_checkpoint(share, source_revision=1)
    assert json.loads((share / 'CURRENT').read_text())['generation'] == generation


def test_partial_save_preserves_current_and_abandoned_generation_grace(snapshot, monkeypatch):
    runtime, share = snapshot
    current = runtime.save_checkpoint(share, source_revision=1)
    baseline = (share / 'CURRENT').read_bytes()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('copy interrupted')
        patch.setattr(checkpoint, '_stream_file', fail)
        with pytest.raises(OSError, match='copy interrupted'):
            runtime.save_checkpoint(share, source_revision=2)
    assert (share / 'CURRENT').read_bytes() == baseline
    abandoned, = [p for p in share.glob('gen-*') if p.name != current]
    assert checkpoint.cleanup_checkpoints(share) == []
    age(abandoned)
    assert checkpoint.cleanup_checkpoints(share) == [abandoned.name]
    assert (share / current).exists()


def test_reader_lock_refuses_concurrent_save_without_changing_current(snapshot):
    runtime, share = snapshot
    runtime.save_checkpoint(share, source_revision=1)
    baseline = (share / 'CURRENT').read_bytes()
    with checkpoint._share_lock(share, exclusive=False):
        with pytest.raises(BlockingIOError):
            runtime.save_checkpoint(share, source_revision=2)
    assert (share / 'CURRENT').read_bytes() == baseline


def test_lock_symlink_never_followed(snapshot, tmp_path):
    runtime, share = snapshot
    share.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('keep')
    (share / '.checkpoint.lock').symlink_to(outside)
    with pytest.raises(OSError):
        runtime.save_checkpoint(share, source_revision=1)
    assert checkpoint.cleanup_checkpoints(share) == []
    assert outside.read_text() == 'keep'


def test_restore_symlink_share_falls_back_without_copying(snapshot, tmp_path):
    runtime, share = snapshot
    runtime.save_checkpoint(share, source_revision=1)
    linked = tmp_path / 'linked'
    linked.symlink_to(share, target_is_directory=True)
    assert LiveFaceIndex.restore_checkpoint(linked, tmp_path / 'restore', source_revision=1) is None