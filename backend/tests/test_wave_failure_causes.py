"""A stage that dead-ends must say why in its failure record.

Failure capsules keep only the exception class (never the message), so the class name is the whole diagnosis.
Both causes used to surface as a bare ``ValueError`` / ``RuntimeError``, which sent an operator to the container
logs to find out why a wave was stuck.
"""
import pytest
pytest.importorskip('faiss')

import wave_assignment as wave
from processing_cleanup import LibraryCleanupFence
from processing_failures import capsule, exception_details
from test_wave_assignment_journal import setup, execute


def test_a_foreign_membership_row_fails_the_stage_as_a_membership_conflict():
    h, snapshots, store, frozen, leases = setup()
    before = set(h.members.rows)
    execute(h, snapshots, store, frozen, leases)
    created = [key for key in h.members.rows if key not in before]
    assert created

    # Same photos again into a library whose membership table already holds rows for those faces,
    # without this library's userId (as a re-created marker row would be).
    h, snapshots, store, frozen, leases = setup()
    for partition, row in created:
        h.members.upsert_entity({'PartitionKey': partition, 'RowKey': row, 'filename': 'a.jpg'})

    with pytest.raises(wave.MembershipConflict) as raised:
        execute(h, snapshots, store, frozen, leases)

    assert isinstance(raised.value, ValueError)
    assert capsule(exception_details(raised.value))['errorClass'] == 'MembershipConflict'


def test_the_cleanup_fence_is_a_named_runtime_error():
    error = LibraryCleanupFence('Processing is fenced by library cleanup')
    assert isinstance(error, RuntimeError)
    assert capsule(exception_details(error))['errorClass'] == 'LibraryCleanupFence'
