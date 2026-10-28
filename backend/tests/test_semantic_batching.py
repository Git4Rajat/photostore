import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest
import processing_metrics as metrics
import processing_diagnostics as diagnostics
import storage_utils as storage
from semantic_batching import text_batches, current_batcher
from test_processing_diagnostics import records


def test_parallel_text_requests_share_one_batch_and_only_wave_accounts_native_work():
    seen = []
    def encode(texts):
        seen.append(texts)
        metrics.add(text_inference_ms=10)
        return [[len(text)] for text in texts]
    with metrics.accounting() as wave, text_batches(encode, lambda _: pytest.fail('Fallback'), window=.03):
        def request(text):
            with metrics.accounting() as image:
                result = current_batcher().submit(text)
            assert image.snapshot()['text_inference_ms'] == 0
            return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, request, s) for s in ('a', 'bb')]
            assert [f.result() for f in futures] == [[1], [2]]
    assert len(seen) == 1 and sorted(seen[0]) == ['a', 'bb']
    assert wave.snapshot()['text_inference_ms'] == 10
    assert wave.snapshot()['semantic_text_batches'] == 1
    assert wave.snapshot()['semantic_text_requests'] == 2
    assert current_batcher() is None


def test_single_text_and_empty_batch_fallback_do_not_deadlock():
    with text_batches(lambda _: [], lambda text: [len(text)], window=.001):
        assert current_batcher().submit('abc') == [3]


def test_encoder_exception_reaches_caller_and_scope_closes():
    def fail(_): raise RuntimeError('encoder failed')
    with pytest.raises(RuntimeError, match='encoder failed'):
        with text_batches(fail, fail, window=0): current_batcher().submit('a')
    assert current_batcher() is None


def test_phase_storage_counters_exclude_overlapping_sibling(caplog):
    barrier = threading.Barrier(2)
    def work(phase, count):
        with diagnostics.apply_phase('owner', 'file.jpg', phase):
            barrier.wait(5)
            for _ in range(count):
                metrics.record_io('table', 'GET', "/photofaces(PartitionKey='p',RowKey='k')", 200, 2, 10)
    with metrics.accounting() as wave, ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(copy_context().run, work, 'face_persistence', 2)
        b = pool.submit(copy_context().run, work, 'artifact_capture', 7)
        a.result(); b.result()
    rows = [r for r in records(caplog) if r['event'] == 'processing_apply_phase']
    assert sorted(r['storage_calls'] for r in rows) == [2, 7]
    assert sorted(r['io_photofaces_read_calls'] for r in rows) == [2, 7]
    assert all(r['storage_attribution'] == 'phase_context' for r in rows)
    assert wave.snapshot()['storage_calls'] == 9


@pytest.fixture
def semantic_world(monkeypatch):
    monkeypatch.setattr(storage, '_build_client_semantic_text', lambda filename, row: row.get('input', 'same'))
    monkeypatch.setattr(storage, 'build_semantic_layers', lambda *a: {})
    monkeypatch.setattr(storage.vision_utils, 'get_text_embedding_version', lambda: 'model-v1')
    monkeypatch.setattr(storage.vision_utils, 'get_text_embedding_dimension', lambda: 2)
    monkeypatch.setitem(storage._CTX, 'embeddings_table_client', None)
    return dict(PartitionKey='lib', RowKey='file.jpg', semanticText='same',
                semanticTextDigest=hashlib.sha256(b'same').hexdigest(),
                semanticEmbeddingVersion='model-v1', semanticEmbedding='[1,0]')


def test_same_valid_semantic_input_reuses_embedding(semantic_world, monkeypatch):
    monkeypatch.setattr(storage.vision_utils, 'encode_text_embedding', lambda _: pytest.fail('Reencoded'))
    with metrics.accounting() as counter: storage._refresh_semantic_fields('file.jpg', semantic_world)
    assert json.loads(semantic_world['semanticEmbedding']) == [1, 0]
    assert counter.snapshot()['semantic_embedding_reuses'] == 1


@pytest.mark.parametrize('change', [dict(input='different'), dict(semanticEmbeddingVersion='old'),
    dict(semanticEmbedding='[]'), dict(semanticEmbedding='[1]'), dict(semanticEmbedding='[0,0]'),
    dict(semanticEmbedding='[NaN,0]'), dict(semanticTextDigest='wrong')])
def test_changed_or_invalid_semantic_result_reencodes(semantic_world, monkeypatch, change):
    semantic_world.update(change)
    seen = []
    monkeypatch.setattr(storage.vision_utils, 'encode_text_embedding', lambda text: (seen.append(text), [0, 1])[1])
    storage._refresh_semantic_fields('file.jpg', semantic_world)
    assert len(seen) == 1 and json.loads(semantic_world['semanticEmbedding']) == [0, 1]


def test_persisted_vector_must_exist_before_semantic_reuse(semantic_world, monkeypatch):
    from azure.core.exceptions import ResourceNotFoundError
    class Missing:
        def get_entity(self, *a): raise ResourceNotFoundError('missing')
    monkeypatch.setitem(storage._CTX, 'embeddings_table_client', Missing())
    monkeypatch.setattr(storage.vision_utils, 'encode_text_embedding', lambda _: [0, 1])
    storage._refresh_semantic_fields('file.jpg', semantic_world)
    assert json.loads(semantic_world['semanticEmbedding']) == [0, 1]


def test_batch_failure_falls_back_individually_without_poisoning_siblings():
    def fallback(text):
        if text == 'bad': raise ValueError('one bad input')
        return [1]
    with text_batches(lambda _: [], fallback, window=.03):
        with ThreadPoolExecutor(max_workers=2) as pool:
            bad = pool.submit(copy_context().run, current_batcher().submit, 'bad')
            good = pool.submit(copy_context().run, current_batcher().submit, 'good')
            with pytest.raises(ValueError, match='one bad input'): bad.result()
            assert good.result() == [1]


def test_persisted_semantic_digest_is_moved_with_vector(semantic_world, monkeypatch):
    written = []
    class Table:
        def upsert_entity(self, row): written.append(row)
    monkeypatch.setitem(storage._CTX, 'embeddings_table_client', Table())
    storage._extract_and_store_embeddings('lib', 'file.jpg', semantic_world)
    assert len(written) == 1
    assert written[0]['semanticTextDigest'] == hashlib.sha256(b'same').hexdigest()
    assert json.loads(written[0]['semanticEmbedding']) == [1, 0]
    assert 'semanticTextDigest' not in semantic_world


def test_phase_transport_categories_are_bounded_and_distinguish_expected_absence():
    with metrics.accounting() as counter:
        metrics.record_io('table', 'GET', '/private-user-table()', 404, 2, 0)
        metrics.record_io('table', 'PATCH', '/photometadata(PartitionKey=x)', 412, 3, 0)
        metrics.record_io('blob', 'PUT', '/private-filename.jpg', 503, 4, 0)
    values = counter.snapshot()
    assert values['storage_not_found'] == values['storage_precondition_failed'] == values['storage_server_errors'] == 1
    assert values['io_other_table_read_calls'] == values['io_photometadata_write_calls'] == values['io_blob_write_calls'] == 1
    assert 'private' not in str(values)


def test_semantic_refresh_uses_batch_encoder_and_preserves_per_source_digest(semantic_world, monkeypatch):
    import vision_utils
    seen = []
    def encode(texts):
        seen.append(texts)
        return [[1, 0] for _ in texts]
    rows = [dict(semantic_world, input=text) for text in ('first', 'second')]
    with text_batches(encode, lambda _: pytest.fail('Singleton fallback'), window=.03):
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(copy_context().run, storage._refresh_semantic_fields, 'file.jpg', row) for row in rows]
            for task in tasks: task.result()
    assert len(seen) == 1 and sorted(seen[0]) == ['first', 'second']
    assert all(json.loads(row['semanticEmbedding']) == [1, 0] for row in rows)
    assert [row['semanticTextDigest'] for row in rows] == [hashlib.sha256(text.encode()).hexdigest() for text in ('first', 'second')]


def test_batch_capacity_is_bounded_under_concurrent_requests():
    sizes = []
    def encode(texts):
        sizes.append(len(texts)); return [[int(text)] for text in texts]
    with text_batches(encode, lambda _: pytest.fail('Fallback'), window=.03, batch_size=3):
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = [pool.submit(copy_context().run, current_batcher().submit, str(i)) for i in range(8)]
            assert [task.result() for task in tasks] == [[i] for i in range(8)]
    assert sum(sizes) == 8 and max(sizes) <= 3


def test_runtime_defaults_two_and_direct_mode_bypasses_broker(monkeypatch):
    from semantic_batching import runtime_config
    monkeypatch.delenv('IPWORK_SEMANTIC_TEXT_BATCH_SIZE', raising=False)
    assert runtime_config()['batch_size']==2
    monkeypatch.setenv('IPWORK_SEMANTIC_TEXT_MODE','direct')
    with text_batches(lambda _: pytest.fail('Broker ran'), lambda _: [1]):
        assert current_batcher() is None


@pytest.mark.parametrize('name,value',[
    ('IPWORK_SEMANTIC_TEXT_MODE','bad'),('IPWORK_SEMANTIC_TEXT_BATCH_SIZE','9'),
    ('IPWORK_SEMANTIC_TEXT_WINDOW_MS','-1')])
def test_bad_runtime_config_rejected(monkeypatch,name,value):
    from semantic_batching import runtime_config
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError):runtime_config()


def test_batch_occupancy_and_wait_metrics_are_recorded():
    with metrics.accounting() as counter,text_batches(lambda texts:[[1] for _ in texts],lambda _: [1],window=.02):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(copy_context().run,current_batcher().submit,str(i)) for i in range(2)]
            for future in futures:assert future.result()==[1]
    values=counter.snapshot()
    assert values['semantic_text_batch_items']==2
    assert values['semantic_text_batch_peak']==2
    assert values['semantic_text_queue_ms']>=0 and values['semantic_text_service_ms']>=0
