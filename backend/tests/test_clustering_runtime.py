"""Real FAISS live runtime tests; no application/store/FAISS search mocks."""
from dataclasses import replace
import sqlite3

import numpy as np
import pytest

faiss = pytest.importorskip('faiss')
import clustering_index as ci
from clustering_runtime import LiveFaceIndex, RebuildNeeded


def row(face, person, vector, tier='5pt', version='v1'):
    return dict(faceId=face, personId=person, embedding=vector,
                tier=tier, embeddingVersion=version)


@pytest.fixture
def runtime(tmp_path):
    index = LiveFaceIndex(tmp_path, candidates=2)
    yield index
    index.close()


def test_flat_scores_and_distinct_person_margin(runtime):
    runtime.build(iter([row('a', 'alice', [1, 0]), row('aa', 'alice', [.99, .01]),
                        row('b', 'bob', [.8, .6]), row('c', 'carol', [0, 1])]))
    best, second, person = runtime.best_two([10, 0], '5pt', 'v1')
    assert (best, second) == pytest.approx((1, .8))
    assert person == 'alice'
    assert isinstance(runtime._base[('5pt', 'v1', 2)], faiss.IndexFlatIP)
    assert runtime._db.execute('SELECT typeof(vector), length(vector) FROM faces').fetchone() == ('blob', 8)


def test_delta_updates_hide_base_and_change_owner(runtime):
    runtime.build([row('a', 'old', [1, 0]), row('b', 'bob', [.8, .6])])
    runtime.upsert('a', 'new', [0, 1], '5pt', 'v1')
    assert runtime.best_two([1, 0], '5pt', 'v1') == pytest.approx((.8, 0, 'bob'))
    assert runtime.best_two([0, 1], '5pt', 'v1')[2] == 'new'
    runtime.upsert('a', 'latest', [1, 0], '5pt', 'v1')
    assert runtime._delta_count == 2
    assert sum(i.ntotal for i in runtime._delta.values()) == 2
    assert runtime.best_two([1, 0], '5pt', 'v1')[2] == 'latest'
    runtime.rebuild()
    assert not runtime._delta and not runtime.needs_rebuild
    assert runtime.best_two([1, 0], '5pt', 'v1')[2] == 'latest'


def test_compatibility_isolation_base_and_delta(runtime):
    runtime.build([row('a', 'tier-person', [1, 0]),
                   row('b', 'other-tier', [1, 0], tier='2pt'),
                   row('c', 'other-version', [1, 0], version='v2'),
                   row('d', 'other-dimension', [1, 0, 0])])
    assert runtime.best_two([1, 0], '5pt', 'v1')[2] == 'tier-person'
    assert runtime.best_two([1, 0], '2pt', 'v1')[2] == 'other-tier'
    assert runtime.best_two([1, 0], '5pt', 'v2')[2] == 'other-version'
    assert runtime.best_two([1, 0, 0], '5pt', 'v1')[2] == 'other-dimension'
    assert runtime.best_two([1, 0], 'unknown', 'v1') == (0, 0, None)
    assert runtime.best_two([1, 0], '5pt', 'unknown') == (0, 0, None)
    assert runtime.best_two([1, 0, 0, 0], '5pt', 'v1') == (0, 0, None)
    runtime.upsert('a', 'moved', [1, 0, 0], 'new-tier', 'v3')
    assert runtime.best_two([1, 0], '5pt', 'v1') == (0, 0, None)
    assert runtime.best_two([1, 0, 0], 'new-tier', 'v3')[2] == 'moved'
    runtime.upsert('a', 'moved-again', [1, 0], 'newer-tier', 'v4')
    assert runtime.best_two([1, 0, 0], 'new-tier', 'v3') == (0, 0, None)
    assert len(runtime._delta) == 2


def test_fresh_validator_rejection_stale_ownership_and_errors(runtime):
    runtime.build([row('a', 'alice', [1, 0]), row('b', 'bob', [.8, .6])])
    runtime.upsert('c', 'carol', [.9, .1], '5pt', 'v1')
    rejected = {'a', 'c'}
    calls = []
    def validate(face, person):
        calls.append((face, person))
        return face not in rejected and person == {'a': 'renamed', 'b': 'bob', 'c': 'carol'}[face]
    assert runtime.best_two([1, 0], '5pt', 'v1', validate)[2] == 'bob'
    assert len(calls) == len(set(calls))
    rejected.clear()
    assert runtime.best_two([1, 0], '5pt', 'v1', validate)[2] == 'carol'
    assert runtime.best_two([1, 0], '5pt', 'v1', lambda *_: False) == (0, 0, None)
    def failed(*_):
        raise RuntimeError('authority unavailable')
    with pytest.raises(RuntimeError, match='authority unavailable'):
        runtime.best_two([1, 0], '5pt', 'v1', failed)


def test_removal_and_recreation_never_resurrect_base(runtime):
    runtime.build([row('a', 'old', [1, 0]), row('b', 'bob', [.8, .6])])
    runtime.remove('a')
    runtime.remove('missing')
    assert runtime.best_two([1, 0], '5pt', 'v1')[2] == 'bob'
    runtime.upsert('a', 'recreated', [0, 1], '5pt', 'v1')
    assert runtime.best_two([1, 0], '5pt', 'v1')[2] == 'bob'
    runtime.remove('a')
    assert runtime._delta_count == 1
    assert sum(i.ntotal for i in runtime._delta.values()) == 1
    runtime.remove('b')
    assert runtime.best_two([1, 0], '5pt', 'v1') == (0, 0, None)


def test_adaptive_overfetch_base_and_delta(runtime):
    runtime.build(row(str(i), 'alice', [1, i / 10000]) for i in range(100))
    runtime.upsert('bob', 'bob', [.8, .6], '5pt', 'v1')
    assert runtime.best_two([1, 0], '5pt', 'v1')[:2] == pytest.approx((1, .8))
    runtime.rebuild()
    assert runtime.best_two([1, 0], '5pt', 'v1')[:2] == pytest.approx((1, .8))


def test_adaptive_bound_is_1024(runtime):
    runtime.build([*(row(str(i), 'alice', [1, 0]) for i in range(1100)),
                   row('bob', 'bob', [.8, .6])])
    visited = []
    assert runtime.best_two([1, 0], '5pt', 'v1', lambda f, p: visited.append(f) or True) == (1, 1, 'alice')
    assert len(visited) == 1


def test_remote_validation_only_reads_top_distinct_people(tmp_path):
    with_index = LiveFaceIndex(tmp_path, candidates=64)
    try:
        with_index.build(row(str(i), str(i), [1, i / 100]) for i in range(100))
        reads = []
        result = with_index.best_two([1, 0], '5pt', 'v1',
                                     lambda face, person: reads.append(face) or True)
        assert result[2] == '0'
        assert len(reads) == 2
    finally:
        with_index.close()


def test_invalid_top_hit_does_not_hide_lower_valid_exemplar(tmp_path):
    index = LiveFaceIndex(tmp_path, candidates=64)
    try:
        index.build([row('rejected', 'alice', [1, 0]),
                     row('valid', 'alice', [.99, .01]),
                     row('bob', 'bob', [.8, .6]),
                     row('carol', 'carol', [0, 1])])
        reads = []
        def validate(face, person):
            reads.append(face)
            return face != 'rejected'
        best, second, pid = index.best_two([1, 0], '5pt', 'v1', validate)
        assert pid == 'alice' and second == pytest.approx(.8)
        assert best > .99
        assert reads == ['rejected', 'valid', 'bob']
    finally:
        index.close()


def test_global_delta_cap_precedes_mutation_and_retry(tmp_path):
    index = LiveFaceIndex(tmp_path, delta_limit=2)
    try:
        index.upsert('a', 'alice', [1, 0], '5pt', 'v1')
        index.upsert('b', 'bob', [1, 0, 0], '2pt', 'v2')
        assert index.needs_rebuild
        with pytest.raises(RebuildNeeded, match='retry'):
            index.upsert('c', 'carol', [0, 1], '5pt', 'v1')
        assert index._old('c') is None and index._delta_count == 2
        with pytest.raises(RebuildNeeded):
            index.upsert('a', 'renamed', [1, 0], '5pt', 'v1')
        assert index.best_two([1, 0], '5pt', 'v1')[2] == 'alice'
        index.rebuild()
        assert not index.needs_rebuild
        index.upsert('c', 'carol', [0, 1], '5pt', 'v1')
        assert index._delta_count == 1
    finally:
        index.close()


@pytest.mark.parametrize('bad', [[], [0, 0], [float('nan'), 0], [float('inf'), 1],
                               [[1, 0]], 'not JSON', None])
def test_invalid_vectors_skip_without_overwriting(runtime, bad):
    runtime.build([row('bad', 'bad', bad), row('good', 'alice', '[1, 0]')])
    runtime.upsert('good', 'wrong', bad, '5pt', 'v1')
    assert runtime.best_two(bad, '5pt', 'v1') == (0, 0, None)
    assert runtime.best_two([1, 0], '5pt', 'v1') == (1, 0, 'alice')
    assert runtime._delta_count == 0


def test_generator_failure_duplicate_and_training_failure_atomic(runtime):
    runtime.build([row('original', 'alice', [1, 0])])
    def failing():
        yield row('new', 'bob', [0, 1])
        raise RuntimeError('source failed')
    with pytest.raises(RuntimeError, match='source failed'):
        runtime.build(failing())
    with pytest.raises(sqlite3.IntegrityError):
        runtime.build([row('duplicate', 'a', [1, 0]), row('duplicate', 'b', [0, 1])])
    original = runtime.config
    runtime.config = replace(original, flat_max_vectors=0)
    with pytest.raises(ValueError, match='Insufficient'):
        runtime.rebuild()
    runtime.config = original
    assert runtime.best_two([1, 0], '5pt', 'v1') == (1, 0, 'alice')
    assert runtime._db.execute('SELECT face_id FROM faces').fetchall() == [('original',)]


def test_real_ivfpq_stream_train_add_exact_rerank_and_no_query_training(tmp_path, monkeypatch):
    config = ci.IndexConfig(flat_max_vectors=10, nlist_min=4, nlist_max=4,
                            pq_subquantizers=2, pq_bits=4, training_sample_size=624,
                            training_iterations=2)
    rng = np.random.default_rng(41)
    vectors = rng.normal(size=(700, 8)).astype('float32')
    index = LiveFaceIndex(tmp_path, config=config, candidates=64, nprobe=64, threads=12)
    try:
        index.build(row(str(i), f'p{i}', v) for i, v in enumerate(vectors))
        base = index._base[('5pt', 'v1', 8)]
        assert isinstance(base, faiss.IndexIVFPQ)
        assert base.is_trained and base.ntotal == 700
        assert base.nprobe == base.nlist == 4 and base.use_precomputed_table == -1
        def forbidden(*_, **__):
            raise AssertionError('query must not build/train')
        monkeypatch.setattr(ci, 'build_face_index', forbidden)
        best, second, person = index.best_two(vectors[0], '5pt', 'v1')
        assert person == 'p0' and best == pytest.approx(1, abs=1e-6)
        # Exact runner-up among ANN candidates; retrieve the same candidate set.
        query = ci.normalized_vector(vectors[0])
        _, ids = base.search(query.reshape(1, -1), 64)
        expected = sorted((float(query @ ci.normalized_vector(vectors[int(i)]))
                           for i in ids[0] if i >= 0), reverse=True)
        assert second == pytest.approx(expected[1], abs=1e-6)
        assert faiss.omp_get_max_threads() <= 2
    finally:
        index.close()


def test_disk_cold_reopen_preserves_mutations_without_checkpoint(tmp_path):
    index = LiveFaceIndex(tmp_path)
    index.build([row('a', 'alice', [1, 0]), row('b', 'bob', [.8, .6])])
    index.upsert('a', 'carol', [0, 1], '5pt', 'v1')
    index.remove('b')
    index.upsert('d', 'dan', [1, 0], '5pt', 'v1')
    expected = index.best_two([1, 0], '5pt', 'v1')
    index.close()
    index.close()
    reopened = LiveFaceIndex(tmp_path)
    try:
        assert reopened.best_two([1, 0], '5pt', 'v1') == expected
        assert not reopened._delta and not reopened.needs_rebuild
        assert reopened._db.execute('SELECT COUNT(*) FROM faces').fetchone()[0] == 2
    finally:
        reopened.close()
    with pytest.raises(RuntimeError, match='closed'):
        reopened.best_two([1, 0], '5pt', 'v1')


def test_aggregate_budget_and_failed_delta_admission_are_atomic(runtime):
    runtime.build([row('a', 'alice', [1, 0])])
    original = runtime.config
    runtime.config = replace(original, memory_budget_bytes=runtime._SQLITE_BYTES + runtime._base_bytes + 1)
    with pytest.raises(ValueError, match='memory budget'):
        runtime.upsert('b', 'bob', [1, 0], '5pt', 'v1')
    assert runtime._old('b') is None and runtime._delta_count == 0
    with pytest.raises(ValueError, match='memory budget'):
        runtime.build([row('other', 'bob', [1, 0], tier='other')])
    runtime.config = original
    assert runtime.best_two([1, 0], '5pt', 'v1') == (1, 0, 'alice')


def test_no_match_single_match_negative_and_deterministic_tie(runtime):
    assert runtime.best_two([1, 0], '5pt', 'v1') == (0, 0, None)
    runtime.upsert('a', 'alice', [-1, 0], '5pt', 'v1')
    assert runtime.best_two([1, 0], '5pt', 'v1') == (-1, 0, 'alice')
    runtime.upsert('b', 'bob', [-1, 0], '5pt', 'v1')
    assert runtime.best_two([1, 0], '5pt', 'v1') == (-1, -1, 'alice')


@pytest.mark.parametrize('kwargs', [dict(delta_limit=0), dict(candidates=0), dict(nprobe=0),
                                   dict(threads=0), dict(delta_limit=True)])
def test_invalid_configuration(tmp_path, kwargs):
    with pytest.raises(ValueError):
        LiveFaceIndex(tmp_path, **kwargs)


def test_warm_upsert_appends_one_vector_without_dirty_scan(runtime, monkeypatch):
    for i in range(30):
        runtime.upsert(str(i), str(i), [1, i / 100], '5pt', 'v1')
    key = ('5pt', 'v1', 2)
    native = runtime._delta[key]
    statements, batches = [], []
    add = faiss.IndexFlatIP.add
    def tracked_add(index, vectors):
        batches.append(vectors.shape)
        return add(index, vectors)
    monkeypatch.setattr(faiss.IndexFlatIP, 'add', tracked_add)
    runtime._db.set_trace_callback(statements.append)
    try:
        runtime.upsert('0', 'updated', [0, 1], '5pt', 'v1')
        runtime.upsert('new', 'new', [-1, 0], '5pt', 'v1')
        runtime.remove('1')
    finally:
        runtime._db.set_trace_callback(None)
    assert runtime._delta[key] is native and native.ntotal == 32
    assert runtime._delta_count == 32
    assert batches == [(1, 2), (1, 2)]
    selects = [sql.lower() for sql in statements if sql.lstrip().lower().startswith('select')]
    assert not selects  # INSERT ... SELECT is a single face-ID lookup, not a scan.
    assert not any('delete from delta_slots' in sql.lower() for sql in statements)
    assert runtime._db.execute('SELECT COUNT(*) FROM delta_slots').fetchone()[0] == 32


def test_obsolete_revision_slots_are_not_reranked_or_validated(runtime):
    runtime.upsert('a', 'old', [1, 0], '5pt', 'v1')
    runtime.upsert('a', 'new', [0, 1], '5pt', 'v1')
    runtime.upsert('b', 'bob', [.8, .6], '5pt', 'v1')
    calls = []
    result = runtime.best_two([1, 0], '5pt', 'v1', lambda f, p: calls.append((f, p)) or True)
    assert result == pytest.approx((.8, 0, 'bob'))
    assert sorted(calls) == [('a', 'new'), ('b', 'bob')]
    assert runtime._db.execute('SELECT slot, revision FROM delta_slots ORDER BY slot').fetchall() == [
        (0, 1), (1, 2), (2, 1)]


def test_updates_and_removals_consume_global_native_slot_cap(tmp_path):
    with_index = LiveFaceIndex(tmp_path, delta_limit=3)
    try:
        with_index.upsert('a', 'alice', [1, 0], '5pt', 'v1')
        with_index.upsert('a', 'alice', [0, 1], '5pt', 'v1')
        with_index.remove('a')
        assert with_index._delta_count == 2 and not with_index.needs_rebuild
        with_index.upsert('a', 'recreated', [1, 0, 0], '2pt', 'v2')
        assert with_index._delta_count == 3 and with_index.needs_rebuild
        with pytest.raises(RebuildNeeded):
            with_index.upsert('a', 'wrong', [1, 0], '5pt', 'v1')
        assert with_index.best_two([1, 0], '5pt', 'v1') == (0, 0, None)
        assert with_index.best_two([1, 0, 0], '2pt', 'v2') == (1, 0, 'recreated')
        with_index.rebuild()
        assert with_index._delta_count == 0 and not with_index.needs_rebuild
    finally:
        with_index.close()


class FailingConnection:
    """Inject one SQL failure while retaining a real SQLite transaction."""
    def __init__(self, connection, statement):
        self.connection = connection
        self.statement = statement

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args):
        if self.statement is not None and sql.lstrip().startswith(self.statement):
            self.statement = None
            raise sqlite3.OperationalError('injected SQL failure')
        return self.connection.execute(sql, *args)


@pytest.mark.parametrize('statement,slots', [('INSERT INTO faces', 1),
                                            ('INSERT INTO delta_slots', 1), ('COMMIT', 2)])
@pytest.mark.parametrize('new_group', [False, True])
def test_sql_failure_keeps_native_tombstones_unmapped(runtime, statement, slots, new_group):
    runtime.upsert('a', 'original', [1, 0], '5pt', 'v1')
    key = ('2pt' if new_group else '5pt', 'v1', 2)
    connection = runtime._db
    runtime._db = FailingConnection(connection, statement)
    try:
        with pytest.raises(sqlite3.OperationalError, match='injected'):
            runtime.upsert('a', 'failed', [0, 1], *key[:2])
    finally:
        runtime._db = connection
    assert not connection.in_transaction
    assert runtime._delta_count == slots == sum(i.ntotal for i in runtime._delta.values())
    assert connection.execute('SELECT COUNT(*) FROM delta_slots').fetchone()[0] == 1
    assert runtime.best_two([1, 0], '5pt', 'v1') == (1, 0, 'original')
    runtime.upsert('a', 'latest', [0, 1], *key[:2])
    assert runtime.best_two([0, 1], *key[:2]) == (1, 0, 'latest')
    assert connection.execute('''SELECT slot FROM delta_slots
        WHERE tier=? AND version=? AND dimension=? ORDER BY slot DESC''', key).fetchone()[0] == (
            slots - 1 if new_group else slots)


@pytest.mark.parametrize('append_first', [False, True])
@pytest.mark.parametrize('existing_face', [False, True])
def test_native_add_failure_rolls_back_sql_without_reusing_slots(runtime, monkeypatch,
                                                                 append_first, existing_face):
    runtime.upsert('a', 'original', [1, 0], '5pt', 'v1')
    native = runtime._delta[('5pt', 'v1', 2)]
    add = faiss.IndexFlatIP.add
    def failing_add(index, vectors):
        if append_first:
            add(index, vectors)
        raise RuntimeError('native add failed')
    face = 'a' if existing_face else 'b'
    with monkeypatch.context() as patch:
        patch.setattr(faiss.IndexFlatIP, 'add', failing_add)
        with pytest.raises(RuntimeError, match='native add failed'):
            runtime.upsert(face, 'failed', [0, 1], '5pt', 'v1')
    assert runtime._delta[('5pt', 'v1', 2)] is native
    assert runtime.needs_rebuild
    assert runtime._delta_count == native.ntotal == 1 + int(append_first)
    assert runtime._db.execute('SELECT slot FROM delta_slots').fetchall() == [(0,)]
    assert runtime.best_two([0, 1], '5pt', 'v1') == (0, 0, 'original')
    runtime.upsert(face, 'latest', [0, 1], '5pt', 'v1')
    assert runtime.best_two([0, 1], '5pt', 'v1') == (1, 0, 'latest')
    assert runtime._db.execute('SELECT MAX(slot) FROM delta_slots').fetchone()[0] == 1 + int(append_first)


def test_removed_slots_still_enforce_memory_admission(runtime):
    runtime.upsert('a', 'alice', [1, 0], '5pt', 'v1')
    runtime.remove('a')
    original = runtime.config
    amount = (runtime._SQLITE_BYTES + runtime._base_bytes + runtime._delta_bytes() +
              8 * 4 + 4096 + 256 + runtime._MAX_K * 2 * 4 * 4)
    runtime.config = replace(original, memory_budget_bytes=amount - 1)
    try:
        with pytest.raises(ValueError, match='memory budget'):
            runtime.upsert('b', 'bob', [0, 1], '5pt', 'v1')
        assert runtime._old('b') is None and runtime._delta_count == 1
    finally:
        runtime.config = original


def test_reopen_migrates_legacy_delta_schema(tmp_path):
    index = LiveFaceIndex(tmp_path)
    index.upsert('a', 'alice', [1, 0], '5pt', 'v1')
    index.close()
    with sqlite3.connect(tmp_path / 'faces.sqlite3') as connection:
        connection.executescript('''
            ALTER TABLE delta_slots RENAME TO old_delta_slots;
            CREATE TABLE delta_slots (
                tier TEXT NOT NULL, version TEXT NOT NULL, dimension INTEGER NOT NULL,
                slot INTEGER NOT NULL, face_row INTEGER NOT NULL,
                PRIMARY KEY(tier, version, dimension, slot)
            ) WITHOUT ROWID;
            INSERT INTO delta_slots SELECT tier, version, dimension, slot, face_row FROM old_delta_slots;
            DROP TABLE old_delta_slots;
        ''')
    reopened = LiveFaceIndex(tmp_path)
    try:
        assert reopened.best_two([1, 0], '5pt', 'v1') == (1, 0, 'alice')
        assert reopened._delta_count == 0
        reopened.upsert('a', 'new', [0, 1], '5pt', 'v1')
        assert reopened.best_two([0, 1], '5pt', 'v1') == (1, 0, 'new')
    finally:
        reopened.close()