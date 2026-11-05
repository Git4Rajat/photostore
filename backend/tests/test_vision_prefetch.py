from concurrent.futures import ThreadPoolExecutor
import os

import pytest

import processing_metrics as metrics
from vision_prefetch import MAX_PREVIEW_BYTES, PrefetchUnavailable, PreviewPrefetch, prefetched_preview


class Metadata:
    library = 'lib'

    def __init__(self, count=8):
        self.rows = {str(index): dict(anonymousImageId=str(index), processingRevision='r1',
            fileHash='hash', preview_status='done', ocr_status='pending', face_status='done',
            ai_vision_status='no_data') for index in range(count)}

    def get_entity(self, library, filename):
        return self.rows[filename]

    def members(self):
        return [dict(photoId=name, processingRevision='r1', filename=name) for name in self.rows]


@pytest.mark.parametrize('storage', ['memory', 'disk'])
def test_parallel_consumption_respects_limits_and_cleans_buffers(storage):
    metadata = Metadata()
    downloaded = []
    def load(row, limit):
        assert limit == MAX_PREVIEW_BYTES
        downloaded.append(row['anonymousImageId'])
        return row['anonymousImageId'].encode()
    with metrics.accounting() as counter, PreviewPrefetch(load, storage=storage) as prefetch:
        prefetch.prepare(metadata.members(), metadata)
        directory = prefetch.directory.name if prefetch.directory else None
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(prefetch.take, 'lib', name, row) for name, row in metadata.rows.items()]
            assert [future.result() for future in futures] == [name.encode() for name in metadata.rows]
        assert prefetch.reserved == 0
    assert len(downloaded) == 8
    if directory:
        assert not os.path.exists(directory)
    values = counter.snapshot()
    assert values['preview_prefetch_reserved_peak_bytes'] <= 16 * 1024 * 1024
    assert values['preview_prefetch_slots_peak'] <= 4
    assert prefetched_preview('lib', '0', metadata.rows['0']) is None


def test_changed_claim_binding_never_uses_old_preview():
    metadata = Metadata(1)
    with PreviewPrefetch(lambda *_: b'old') as prefetch:
        prefetch.prepare(metadata.members(), metadata)
        row = dict(metadata.rows['0'], processingRevision='r2')
        assert prefetch.take('lib', '0', row) is None
        assert prefetch.take('different-library', '0', metadata.rows['0']) is None
        assert prefetch.take('lib', '0', metadata.rows['0']) == b'old'


def test_failed_or_oversize_preview_does_not_get_downloaded_twice():
    metadata = Metadata(1)
    calls = []
    def load(*args):
        calls.append(1)
        raise RuntimeError('missing')
    with PreviewPrefetch(load) as prefetch:
        prefetch.prepare(metadata.members(), metadata)
        with pytest.raises(PrefetchUnavailable):
            prefetch.take('lib', '0', metadata.rows['0'])
    assert calls == [1]


def test_finished_and_superseded_sources_are_not_prefetched():
    metadata = Metadata(3)
    metadata.rows['0'].update(ocr_status='done')
    metadata.rows['1'].update(processingRevision='r2')
    metadata.rows['2'].update(processing_state='deleted')
    with PreviewPrefetch(lambda *_: pytest.fail('Unnecessary download')) as prefetch:
        prefetch.prepare(metadata.members(), metadata)
        assert not prefetch.pending


def test_oversize_download_is_discarded_and_tiny_budget_is_respected():
    metadata = Metadata(2)
    with metrics.accounting() as counter, PreviewPrefetch(lambda *_: b'x' * (MAX_PREVIEW_BYTES + 1),
            byte_limit=MAX_PREVIEW_BYTES) as prefetch:
        prefetch.prepare(metadata.members(), metadata)
        assert len(prefetch.pending) == 1
        with pytest.raises(PrefetchUnavailable):
            prefetch.take('lib', '0', metadata.rows['0'])
    assert counter.snapshot()['preview_prefetch_reserved_peak_bytes'] == MAX_PREVIEW_BYTES


def test_bounded_sdk_preview_read_has_no_original_or_head_requests(monkeypatch):
    from types import SimpleNamespace
    import storage_utils as storage
    calls = []
    def download(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(readall=lambda: b'preview')
    monkeypatch.setitem(storage._CTX, 'blob_thumbnail_container', 'thumbs')
    monkeypatch.setattr(storage, '_get_blob_client', lambda container, name:
        SimpleNamespace(download_blob=download) if (container, name) == ('thumbs', 'preview-key') else None)
    assert storage.download_preview_bytes_bounded('preview-key', 20) == b'preview'
    assert calls == [dict(offset=0, length=21, max_concurrency=1,
                          connection_timeout=5, read_timeout=15, retry_total=0)]
