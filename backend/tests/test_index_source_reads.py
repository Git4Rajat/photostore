from contextvars import ContextVar
import threading

import pytest

from index_source_reads import SourceReads


def test_reads_overlap_preserve_context_and_return_in_source_order():
    context = ContextVar('source_test', default='missing')
    token = context.set('owner')
    barrier = threading.Barrier(4, timeout=3)
    seen = []
    lock = threading.Lock()

    def read(key):
        assert context.get() == 'owner'
        context.set(key)  # Each task gets its own copy, even on reused threads.
        with lock:
            seen.append(threading.get_ident())
        barrier.wait()
        return key * 2

    try:
        with SourceReads(workers=4) as reads:
            assert list(reads.map(read, range(8))) == [(i, i * 2) for i in range(8)]
        assert len(set(seen)) == 4
        assert context.get() == 'owner'
    finally:
        context.reset(token)


def test_prefetch_is_bounded_and_early_exit_drains_reads():
    supplied = []
    completed = []

    def keys():
        for key in range(10000):
            supplied.append(key)
            yield key

    def read(key):
        completed.append(key)
        return key

    with SourceReads(workers=4) as reads:
        rows = reads.map(read, keys())
        assert next(rows) == (0, 0)
        assert supplied == [0, 1, 2, 3]
    # No source iterator expansion or background reads after the owner exits.
    rows.close()
    assert set(completed) <= {0, 1, 2, 3}


@pytest.mark.parametrize('workers', [1, 4])
def test_nested_person_face_reads_share_pool_without_deadlock(workers):
    with SourceReads(workers=workers) as reads:
        result = []
        for person, _ in reads.map(lambda key: key, range(6)):
            result.append((person, list(reads.map(lambda face: face * 2, range(8)))))
    assert result == [(person, [(face, face * 2) for face in range(8)]) for person in range(6)]


def test_read_failure_propagates_before_later_results_are_consumed():
    consumed = []

    def read(key):
        if key == 2:
            raise TimeoutError('source unavailable')
        return key

    with pytest.raises(TimeoutError, match='source unavailable'):
        with SourceReads(workers=4) as reads:
            for key, value in reads.map(read, range(100)):
                consumed.append(value)
    assert consumed == [0, 1]


@pytest.mark.parametrize('workers', [0, 17, -1, True, 1.5])
def test_invalid_worker_limits_are_rejected(workers):
    with pytest.raises(ValueError):
        SourceReads(workers=workers)
