import pytest
from azure.core.exceptions import ResourceNotFoundError

from wave_publication import ObligationLedger, compute_obligations
from tests.test_index_work import EtagTable


@pytest.fixture
def ledger():
    return ObligationLedger(EtagTable(), 'wave-1')


def test_compute_obligations_are_distinct_per_scope_wave_and_revision():
    a = compute_obligations('lib', 'wave-1', 'rev-1', ['search', 'people'])
    assert len(set(a.values())) == 2
    b = compute_obligations('lib', 'wave-2', 'rev-1', ['search', 'people'])
    assert set(a.values()).isdisjoint(b.values())
    c = compute_obligations('lib', 'wave-1', 'rev-2', ['search', 'people'])
    assert set(a.values()).isdisjoint(c.values())
    # Same inputs -> same ids, deterministically (needed so a retried
    # computation references the same already-persisted rows).
    assert compute_obligations('lib', 'wave-1', 'rev-1', ['search', 'people']) == a


def test_is_complete_only_once_every_required_obligation_has_a_receipt(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['search', 'people'])
    ledger.record_required(obligations)
    assert ledger.is_complete() is False
    ledger.record_receipt(obligations['search'])
    assert ledger.is_complete() is False
    ledger.record_receipt(obligations['people'])
    assert ledger.is_complete() is True


def test_an_explicit_no_change_receipt_still_satisfies_the_obligation(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['albums'])
    ledger.record_required(obligations)
    ledger.record_receipt(obligations['albums'], no_change=True)
    assert ledger.is_complete() is True


def test_empty_required_set_is_not_reported_complete(ledger):
    # No record_required call at all -- e.g. a crash before the required set
    # was ever captured. Must read as incomplete/unknown, never "done".
    assert ledger.is_complete() is False


def test_record_required_is_idempotent_for_the_identical_set(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['search'])
    ledger.record_required(obligations)
    ledger.record_receipt(obligations['search'])
    ledger.record_required(obligations)  # Re-capture of the identical set.
    assert ledger.is_complete() is True  # The existing receipt survives.


def test_record_required_rejects_a_different_set_once_already_captured(ledger):
    ledger.record_required(compute_obligations('lib', 'wave-1', 'rev-1', ['search']))
    with pytest.raises(RuntimeError, match='differs'):
        ledger.record_required(compute_obligations('lib', 'wave-1', 'rev-1', ['people']))


def test_receipt_for_a_never_required_obligation_is_rejected(ledger):
    with pytest.raises(RuntimeError, match='never required'):
        ledger.record_receipt('unknown-obligation-id')


def test_receipt_is_idempotent_for_duplicate_dispatch(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['search'])
    ledger.record_required(obligations)
    ledger.record_receipt(obligations['search'], output_generation='gen-1')
    ledger.record_receipt(obligations['search'], output_generation='gen-1')  # Duplicate delivery.
    assert ledger.is_complete() is True


def test_a_foreign_waves_receipt_cannot_satisfy_this_waves_obligation():
    table = EtagTable()
    this_wave = ObligationLedger(table, 'wave-1')
    other_wave = ObligationLedger(table, 'wave-2')
    this_wave.record_required(compute_obligations('lib', 'wave-1', 'rev-1', ['search']))
    other_wave.record_required(compute_obligations('lib', 'wave-2', 'rev-1', ['search']))
    other_obligation = compute_obligations('lib', 'wave-2', 'rev-1', ['search'])['search']
    other_wave.record_receipt(other_obligation)
    assert other_wave.is_complete() is True
    assert this_wave.is_complete() is False  # Unaffected by the other wave's receipt.


def test_required_ids_reflects_the_captured_set(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['search', 'people'])
    ledger.record_required(obligations)
    assert ledger.required_ids() == set(obligations.values())
