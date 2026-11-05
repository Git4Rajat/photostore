from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading

import pytest
import processing_metrics as metrics
from image_batching import ImageBatcher, current_batcher, image_batches


def test_parallel_requests_preserve_association_and_account_native_work_once():
    barrier = threading.Barrier(2)
    seen = []
    def encode(items):
        seen.append(items)
        metrics.add(clip_inference_ms=10)
        return [[1., 0.] if item == 'a' else [0., 1.] for item in items]
    with metrics.accounting() as wave, image_batches(encode, lambda _: pytest.fail('Singleton'),
                                                    dimensions=2, window=.05):
        def request(item):
            with metrics.accounting() as image:
                barrier.wait(5)
                vector = current_batcher().submit(item)
            assert image.snapshot()['clip_inference_ms'] == 0
            assert image.snapshot()['clip_image_requests'] == 1
            return vector
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(copy_context().run, request, x) for x in ('a', 'b')]
            assert [f.result(5) for f in futures] == [[1., 0.], [0., 1.]]
    assert len(seen) == 1
    assert wave.snapshot()['clip_inference_ms'] == 10
    assert wave.snapshot()['clip_image_batch_items'] == 2
    assert wave.snapshot()['clip_image_requests'] == 2
    assert current_batcher() is None


def test_singleton_flush_uses_singleton_encoder_and_nested_scope_reuses_worker():
    with image_batches(lambda _: pytest.fail('Stacked singleton'), lambda _: [1., 0.],
                       dimensions=2, window=.001):
        worker = current_batcher()
        with image_batches(None, None):
            assert current_batcher() is worker
            assert worker.submit('one') == [1., 0.]
    assert not worker.thread.is_alive()
    with pytest.raises(RuntimeError, match='closed'):
        worker.submit('late')
    worker.close()


@pytest.mark.parametrize('result', [[], [[1., 0.]], [[float('nan'), 0.], [0., 1.]],
                                    [[0., 0.], [0., 1.]], [[1.], [1.]]])
def test_invalid_batch_falls_back_per_request(result):
    barrier = threading.Barrier(2)
    with metrics.accounting() as wave, image_batches(lambda _: result, lambda _: [1., 0.],
                                                    dimensions=2, window=.05):
        def request():
            barrier.wait(5)
            return current_batcher().submit('item')
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(copy_context().run, request) for _ in range(2)]
            assert all(f.result(5) == [1., 0.] for f in futures)
    assert wave.snapshot()['clip_image_batch_fallbacks'] == 2


def test_failures_reach_callers_and_do_not_strand_following_requests():
    def fail(_):
        raise RuntimeError('inference failed')
    batcher = ImageBatcher(fail, fail, dimensions=2, window=0)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match='inference failed'):
                batcher.submit('bad')
    finally:
        batcher.close()
    assert not batcher.thread.is_alive()


def test_shutdown_drains_pending_request_and_isolates_attempts():
    entered, release = threading.Event(), threading.Event()
    def encode(_):
        entered.set()
        assert release.wait(5)
        return [1., 0.]
    batcher = ImageBatcher(None, encode, dimensions=2, window=0)
    with ThreadPoolExecutor(2) as pool:
        request = pool.submit(batcher.submit, 'item')
        assert entered.wait(5)
        closing = pool.submit(batcher.close)
        release.set()
        assert request.result(5) == [1., 0.]
        closing.result(5)
    assert not batcher.thread.is_alive()
    with image_batches(None, lambda _: [0., 1.], dimensions=2, window=0):
        assert current_batcher() is not batcher
        assert current_batcher().submit('other') == [0., 1.]


def test_single_source_and_default_configuration_do_not_create_broker(monkeypatch):
    import vision_utils as vu
    monkeypatch.delenv('IPWORK_CLIP_IMAGE_BATCH_SIZE', raising=False)
    with vu.image_embedding_batches():
        assert current_batcher() is None
    with vu.image_embedding_batches(source_workers=1, batch_size=2):
        assert current_batcher() is None


@pytest.mark.parametrize('size,window', [(0, .002), (9, .002), (2, -.1), (2, .1)])
def test_invalid_options_rejected(size, window):
    with pytest.raises(ValueError):
        ImageBatcher(None, None, batch_size=size, window=window)
