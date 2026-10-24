import pytest

from execution_lease import ExecutionLeases, OwnershipLost
from tests.test_index_work import EtagTable


class Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


@pytest.fixture
def leases():
    return ExecutionLeases(EtagTable(), clock=Clock(), lease_seconds=100)


def test_first_claim_creates_a_running_record_at_epoch_one(leases):
    ownership = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    assert ownership.epoch == 1 and ownership.status == 'running'
    assert ownership.owner


def test_a_live_claim_rejects_a_concurrent_claim_even_from_the_same_caller(leases):
    leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    with pytest.raises(OwnershipLost, match='already claimed'):
        leases.claim('lib', 'wave-1', 'vision', 'rev-1')


def test_expired_lease_is_reclaimed_with_an_advanced_epoch_and_a_brand_new_owner(leases):
    first = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.clock.value += 1000  # Past lease_seconds.
    second = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    assert second.epoch == first.epoch + 1
    assert second.owner != first.owner
    assert second.executionId == first.executionId  # Same logical execution, same revision.


def test_old_worker_cannot_renew_checkpoint_or_release_after_a_replacement_reclaims(leases):
    first = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.clock.value += 1000
    leases.claim('lib', 'wave-1', 'vision', 'rev-1')  # Replacement reclaims.
    with pytest.raises(OwnershipLost):
        leases.renew(first)
    with pytest.raises(OwnershipLost):
        leases.checkpoint(first, 'progress')
    with pytest.raises(OwnershipLost):
        leases.release(first)
    with pytest.raises(OwnershipLost):
        leases.complete(first, 'receipt')


def test_an_old_workers_finally_block_cannot_release_its_replacements_claim(leases):
    """The exact scenario the target doc calls out: a worker that crashed
    mid-renewal, got reclaimed, and then runs its own cleanup/finally
    handler -- that handler's release() call must raise, not silently drop
    the replacement's live claim."""
    stale = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.clock.value += 1000
    replacement = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    with pytest.raises(OwnershipLost):
        leases.release(stale)
    # The replacement's own claim is untouched by the stale release attempt.
    leases.renew(replacement)


def test_renewal_during_long_work_succeeds_and_keeps_the_same_epoch(leases):
    ownership = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.clock.value += 50  # Within the lease window.
    leases.renew(ownership)
    leases.clock.value += 50  # Would have expired without the renewal.
    leases.checkpoint(ownership, 'halfway')
    assert leases.row(ownership)['progressCheckpoint'] == 'halfway'


def test_renew_after_expiry_raises_rather_than_resurrecting_the_lease(leases):
    ownership = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.clock.value += 1000
    with pytest.raises(OwnershipLost, match='expired'):
        leases.renew(ownership)


def test_release_frees_the_lease_for_a_new_claim_without_bumping_the_epoch_mid_attempt(leases):
    ownership = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.release(ownership)
    reclaim = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    assert reclaim.epoch == ownership.epoch + 1  # A fresh claim, even though it wasn't expired.


def test_complete_is_idempotent_for_a_duplicate_message_on_the_same_revision(leases):
    ownership = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.complete(ownership, 'receipt-1')
    # A duplicate/late message for the same already-finished release must
    # not re-run work: claiming again returns the succeeded record as-is.
    replay = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    assert replay.status == 'succeeded'
    assert leases.row(replay)['completionReceipt'] == 'receipt-1'
    assert replay.owner == ''  # Never reclaimed into a live, running attempt.


def test_a_new_input_revision_supersedes_a_finished_older_revision_with_a_fresh_execution(leases):
    first = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    leases.complete(first, 'receipt-1')
    second = leases.claim('lib', 'wave-1', 'vision', 'rev-2')
    assert second.status == 'running'
    assert second.epoch == 1
    assert second.executionId != first.executionId
    assert second.inputRevision == 'rev-2'


def test_a_new_input_revision_cannot_preempt_a_still_live_older_attempt(leases):
    leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    with pytest.raises(OwnershipLost, match='already claimed'):
        leases.claim('lib', 'wave-1', 'vision', 'rev-2')


def test_fail_clears_ownership_and_is_retried_by_a_later_claim(leases):
    ownership = leases.claim('lib', 'wave-1', 'cluster', 'rev-1')
    leases.fail(ownership, 'boom')
    reclaim = leases.claim('lib', 'wave-1', 'cluster', 'rev-1')
    assert reclaim.status == 'running'  # Failed work is retried, not treated as finished.


def test_invalid_stage_is_rejected():
    with pytest.raises(ValueError):
        ExecutionLeases.key('lib', 'wave-1', 'bogus-stage')


def test_different_stages_for_the_same_wave_are_independent(leases):
    vision = leases.claim('lib', 'wave-1', 'vision', 'rev-1')
    cluster = leases.claim('lib', 'wave-1', 'cluster', 'rev-1')
    leases.renew(vision)
    leases.renew(cluster)


def test_expired_owner_cannot_complete_even_without_a_replacement(leases):
    ownership = leases.claim('lib', 'wave', 'cluster', 'revision')
    leases.clock.value += 101
    with pytest.raises(OwnershipLost, match='expired'):
        leases.complete(ownership, 'receipt')


def test_expired_owner_cannot_mark_failure_without_a_replacement(leases):
    ownership = leases.claim('lib', 'wave', 'cluster', 'revision')
    leases.clock.value += 101
    with pytest.raises(OwnershipLost, match='expired'):
        leases.fail(ownership, 'late failure')


def test_keepalive_renews_while_the_main_thread_is_in_a_blocking_operation(leases,monkeypatch):
    import threading
    ownership=leases.claim('lib','wave','cluster','rev')
    leases.lease_seconds=.03
    renewed=threading.Event()
    original=leases.renew
    def renew(owner):
        original(owner)
        renewed.set()
    monkeypatch.setattr(leases,'renew',renew)
    with leases.keepalive(ownership) as check:
        assert renewed.wait(timeout=1)
        check()
