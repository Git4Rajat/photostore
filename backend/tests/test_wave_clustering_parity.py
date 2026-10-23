"""run_wave_assignment must equal the single-pass oracle, with real crash/
retry resumability via its decision journal -- see docs/indexing-clustering-
target-state.md step 5 ("one-shot clustering")."""
import pytest

pytest.importorskip('faiss')
import faiss_assignment as assignment
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from clustering_wave import ClusteringJournal, canonical_order, run_wave_assignment
from clustering_reference_oracle import assignment_snapshot, run_single_pass_reference
from test_clustering_reference_oracle import assert_fixture_semantics, seeded
from tests.test_index_work import EtagTable


@pytest.fixture(autouse=True)
def clean_runtime():
    assignment.invalidate()
    yield
    assignment.invalidate()


def manifest(h, ids):
    members = {h.faces.rows['u', face_id]['filename']: [face_id] for face_id in ids}
    face_rows = {face_id: h.faces.rows['u', face_id] for face_id in ids}
    return members, face_rows


@pytest.mark.parametrize('chunk_size', [1, 2, 3, 9])
def test_matches_single_pass_oracle_across_chunk_sizes(chunk_size):
    reference, ids = seeded()
    expected = run_single_pass_reference(reference, ids)
    assert_fixture_semantics(expected)
    assignment.invalidate()

    h, ids = seeded()
    members, face_rows = manifest(h, ids)
    journal = ClusteringJournal(EtagTable(), 'wave-1')
    committed = run_wave_assignment('u', h.assigner, journal, members, face_rows, chunk_size=chunk_size)
    assert committed == -(-len(ids) // chunk_size)  # ceil division: every chunk is new

    actual = assignment_snapshot(h)
    assert_fixture_semantics(actual)
    assert actual == expected


def test_rerunning_a_fully_committed_wave_is_a_pure_noop():
    h, ids = seeded()
    members, face_rows = manifest(h, ids)
    journal = ClusteringJournal(EtagTable(), 'wave-1')
    first = run_wave_assignment('u', h.assigner, journal, members, face_rows, chunk_size=3)
    assert first > 0
    before = assignment_snapshot(h)
    second = run_wave_assignment('u', h.assigner, journal, members, face_rows, chunk_size=3)
    assert second == 0
    assert assignment_snapshot(h) == before


@pytest.mark.parametrize('failure', ['person', 'member', 'face'])
def test_resumes_after_a_crash_mid_chunk_without_redoing_committed_chunks(failure):
    reference, ids = seeded()
    expected = run_single_pass_reference(reference, ids)
    assignment.invalidate()

    h, ids = seeded(assignment.AssignmentConfig(coalesce_writes=True))
    members, face_rows = manifest(h, ids)
    journal = ClusteringJournal(EtagTable(), 'wave-1')

    table = {'person': h.people, 'member': h.members, 'face': h.faces}[failure]
    table.fail_write = HttpResponseError('injected commit response lost')
    table.commit_then_fail = True
    with pytest.raises(HttpResponseError, match='commit response lost'):
        run_wave_assignment('u', h.assigner, journal, members, face_rows, chunk_size=3)
    assert not journal.committed(0)
    h.assigner.invalidate()  # No private runtime from the failed attempt survives.

    committed = run_wave_assignment('u', h.assigner, journal, members, face_rows, chunk_size=3)
    assert committed == 3  # every chunk, including the retried first one

    actual = assignment_snapshot(h)
    assert_fixture_semantics(actual)
    assert actual == expected

    # A further call finds everything already committed -- no re-plan, no
    # redone assignment work.
    assert run_wave_assignment('u', h.assigner, journal, members, face_rows, chunk_size=3) == 0


def test_journal_rejects_a_changed_face_set_for_an_already_planned_chunk():
    journal = ClusteringJournal(EtagTable(), 'wave-1')
    journal.plan(0, ['a', 'b'])
    with pytest.raises(RuntimeError, match='changed'):
        journal.plan(0, ['a', 'c'])
    journal.plan(0, ['a', 'b'])  # Identical replan is a no-op.


def test_journal_commit_is_idempotent_and_requires_a_plan_first():
    journal = ClusteringJournal(EtagTable(), 'wave-1')
    with pytest.raises(ResourceNotFoundError):
        journal.commit(0)
    journal.plan(0, ['a'])
    assert journal.committed(0) is False
    journal.commit(0)
    assert journal.committed(0) is True
    journal.commit(0)  # Idempotent.
    assert journal.committed(0) is True


def test_canonical_order_matches_oracle_ordering():
    h, ids = seeded()
    faces = {face_id: h.faces.rows['u', face_id] for face_id in ids}
    from clustering_reference_oracle import canonical_work
    assert tuple(canonical_order(faces)) == canonical_work(h, ids)
