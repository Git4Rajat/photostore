"""Real-matcher chunk/checkpoint/replay comparisons against serial orchestration."""
import pytest

pytest.importorskip('faiss')
import faiss_assignment as assignment
from faiss_assignment import AssignmentConfig
from azure.core.exceptions import HttpResponseError
from test_faiss_assignment import Harness
from clustering_reference_oracle import (
    assignment_snapshot, canonical_work, run_single_pass_reference,
)


@pytest.fixture(autouse=True)
def clean_runtime():
    assignment.invalidate()
    yield
    assignment.invalidate()


def seeded(config=None):
    h = Harness(config=config)
    h.person('alice', ['anchor-a'], rep=[1, 0, 0], name='Alice')
    h.person('bob', ['anchor-b'], rep=[0, 1, 0], name='Bob')
    h.face('anchor-a', [1, 0, 0], person='alice')
    h.face('anchor-b', [0, 1, 0], person='bob')
    rows = (
        ('a', [.99, .01, 0], {}),
        ('b', [0, 0, 1], {}),  # First exemplar of a new identity.
        ('c', [0, .99, .01], {}),
        ('d', [.01, 0, .999], {}),  # Same new identity, nonadjacent input.
        ('e', [.99, .01, 0], {}),
        ('f', [0, .02, .999], {}),
        ('g', [0, 1, 0], {'rejected': True}),
        ('h', [1, 0, 0], {'person': 'alice'}),  # Existing ownership is preserved.
        ('i', [0, 0, 1], {'version': 'v2'}),  # Incompatible model, distinct identity.
    )
    for face_id, vector, extra in rows:
        h.face(face_id, vector, **extra)
        h.faces.rows['u', face_id]['filename'] = f'{face_id}.jpg'
    return h, [face_id for face_id, _, _ in rows]


def assert_fixture_semantics(snapshot):
    owners = snapshot['owners']
    assert owners['a'] == owners['e'] == owners['h'] == 'alice'
    assert owners['c'] == 'bob'
    assert owners['b'] == owners['d'] == owners['f']
    assert owners['b'] not in ('', 'alice', 'bob')
    assert owners['i'] not in ('', owners['b'], 'alice', 'bob')
    assert owners['g'] == ''
    assert snapshot['people']['alice']['name'] == 'Alice'
    assert snapshot['people']['bob']['name'] == 'Bob'


@pytest.mark.parametrize('chunk_size', [1, 2, 3, 9])
@pytest.mark.parametrize('mode', ['warm', 'checkpoint_restart', 'cold_restart', 'replay'])
def test_chunked_actual_matcher_equals_single_pass_oracle(tmp_path, chunk_size, mode):
    reference, ids = seeded()
    expected = run_single_pass_reference(reference, ids)
    assert_fixture_semantics(expected)
    assignment.invalidate()

    config = AssignmentConfig(
        checkpoint_dir=str(tmp_path / 'share') if mode == 'checkpoint_restart' else None,
        checkpoint_interval_seconds=0,
        coalesce_writes=True,
    )
    h, ids = seeded(config)
    work = canonical_work(h, ids)
    for offset in range(0, len(work), chunk_size):
        chunk = work[offset:offset + chunk_size]
        with h.assigner.batch('u', chunk, staged=True) as assign:
            for face_id in chunk:
                assign(h.faces.rows['u', face_id]['filename'], [face_id])
        if mode == 'replay':
            with h.assigner.batch('u', chunk, staged=True) as assign:
                for face_id in chunk:
                    assign(h.faces.rows['u', face_id]['filename'], [face_id])
        if mode in ('checkpoint_restart', 'cold_restart') and offset + chunk_size < len(work):
            if mode == 'checkpoint_restart':
                h.assigner.final_checkpoint()
            h.assigner.invalidate()

    actual = assignment_snapshot(h)
    assert_fixture_semantics(actual)
    assert actual == expected
    if mode == 'checkpoint_restart':
        # Restore must be real, not a silent cold rebuild producing the same result.
        assert len(h.faces.queries) == 1


@pytest.mark.parametrize('failure', ['person', 'member', 'face', 'projection'])
def test_partial_chunk_retry_equals_single_pass_oracle(failure):
    reference, ids = seeded()
    expected = run_single_pass_reference(reference, ids)
    assert_fixture_semantics(expected)
    assignment.invalidate()
    h, ids = seeded(AssignmentConfig(coalesce_writes=True))
    work = canonical_work(h, ids)
    first = work[:3]

    def run_chunk(chunk):
        with h.assigner.batch('u', chunk, staged=True) as assign:
            for face_id in chunk:
                assign(h.faces.rows['u', face_id]['filename'], [face_id])
        return assign.metadata_errors

    if failure == 'projection':
        h.metadata_error = RuntimeError('injected projection failure')
        assert run_chunk(first)
        h.metadata_error = None
    else:
        table = {'person': h.people, 'member': h.members, 'face': h.faces}[failure]
        table.fail_write = HttpResponseError('injected commit response lost')
        table.commit_then_fail = True
        with pytest.raises(HttpResponseError, match='commit response lost'):
            run_chunk(first)
    h.assigner.invalidate()  # No private runtime from the failed attempt survives.
    assert not run_chunk(first)
    assert not run_chunk(work[3:])
    actual = assignment_snapshot(h)
    assert_fixture_semantics(actual)
    assert actual == expected
