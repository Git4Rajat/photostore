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
    ledger.record_receipt(obligations['search'], output_generation='published-v1')
    assert ledger.is_complete() is False
    ledger.record_receipt(obligations['people'], output_generation='published-v1')
    assert ledger.is_complete() is True


def test_an_explicit_no_change_receipt_still_satisfies_the_obligation(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['albums'])
    ledger.record_required(obligations)
    ledger.record_receipt(obligations['albums'], no_change=True, witness='captured input has no changes')
    assert ledger.is_complete() is True


def test_empty_required_set_is_not_reported_complete(ledger):
    # No record_required call at all -- e.g. a crash before the required set
    # was ever captured. Must read as incomplete/unknown, never "done".
    assert ledger.is_complete() is False


def test_record_required_is_idempotent_for_the_identical_set(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['search'])
    ledger.record_required(obligations)
    ledger.record_receipt(obligations['search'], output_generation='published-v1')
    ledger.record_required(obligations)  # Re-capture of the identical set.
    assert ledger.is_complete() is True  # The existing receipt survives.


def test_record_required_rejects_a_different_set_once_already_captured(ledger):
    ledger.record_required(compute_obligations('lib', 'wave-1', 'rev-1', ['search']))
    with pytest.raises(RuntimeError, match='differs'):
        ledger.record_required(compute_obligations('lib', 'wave-1', 'rev-1', ['people']))


def test_receipt_for_a_never_required_obligation_is_rejected(ledger):
    with pytest.raises(RuntimeError, match='never required'):
        ledger.record_receipt('unknown-obligation-id', output_generation='published-v1')


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
    other_wave.record_receipt(other_obligation, output_generation='published-v1')
    assert other_wave.is_complete() is True
    assert this_wave.is_complete() is False  # Unaffected by the other wave's receipt.


def test_required_ids_reflects_the_captured_set(ledger):
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['search', 'people'])
    ledger.record_required(obligations)
    assert ledger.required_ids() == set(obligations.values())


def test_partial_capture_preserves_full_requirement_and_resumes_after_crash(ledger, monkeypatch):
    from azure.core.exceptions import AzureError
    obligations = compute_obligations('lib', 'wave-1', 'rev-1', ['search', 'people'])
    create = ledger.table.create_entity
    def fail_after_first_obligation(row):
        if row['RowKey'] == obligations['people']:
            raise AzureError('crash during capture')
        return create(row)
    monkeypatch.setattr(ledger.table, 'create_entity', fail_after_first_obligation)
    with pytest.raises(AzureError):
        ledger.record_required(obligations)
    assert ledger.required_ids() == set(obligations.values())
    ledger.record_receipt(obligations['search'], output_generation='published-search')
    assert not ledger.is_complete()
    with pytest.raises(RuntimeError, match='differs'):
        ledger.record_required({'search': obligations['search']})
    monkeypatch.setattr(ledger.table, 'create_entity', create)
    ledger.record_required(obligations)
    ledger.record_receipt(obligations['people'], output_generation='published-people')
    assert ledger.is_complete()


def test_explicit_empty_capture_is_distinct_from_missing_capture(ledger):
    assert not ledger.is_complete()
    ledger.record_required({})
    assert ledger.is_complete()
    assert ledger.required_ids() == set()
    with pytest.raises(RuntimeError, match='differs'):
        ledger.record_required(compute_obligations('lib', 'wave-1', 'rev-1', ['people']))


@pytest.mark.parametrize('kwargs', [{}, {'no_change': True}, {'output_generation': ''},
    {'no_change': True, 'output_generation': 'generation', 'witness': 'proof'},
    {'output_generation': 'generation', 'witness': 'proof'}])
def test_receipt_requires_applied_generation_or_explicit_no_change_witness(ledger, kwargs):
    ids = compute_obligations('lib', 'wave-1', 'rev-1', ['search'])
    ledger.record_required(ids)
    with pytest.raises(ValueError):
        ledger.record_receipt(ids['search'], **kwargs)
    assert not ledger.is_complete()


def test_conflicting_receipt_cannot_overwrite_an_existing_generation(ledger):
    ids = compute_obligations('lib', 'wave-1', 'rev-1', ['search'])
    ledger.record_required(ids)
    ledger.record_receipt(ids['search'], output_generation='generation-A')
    with pytest.raises(RuntimeError, match='Conflicting'):
        ledger.record_receipt(ids['search'], output_generation='generation-B')


def test_exact_prerequisites_must_be_satisfied_before_dependent_publication():
    table = EtagTable()
    ledger = ObligationLedger(table, 'wave-1', library='library', frozen_revision='frozen-1')
    deps = {'albums': ['search']}
    ids = compute_obligations('library', 'wave-1', 'frozen-1', ['search', 'albums'], prerequisite_scopes=deps)
    root = ledger.record_required(ids, prerequisite_scopes=deps)
    with pytest.raises(RuntimeError, match='prerequisite'):
        ledger.record_receipt(ids['albums'], output_generation='albums-A')
    ledger.record_receipt(ids['search'], output_generation='search-A')
    ledger.record_receipt(ids['albums'], no_change=True, witness='albums already cover this exact search generation')
    proof = ledger.completion_proof()
    assert proof['requiredSetId'] == root and proof['frozenRevision'] == 'frozen-1'
    different_deps = compute_obligations('library', 'wave-1', 'frozen-1', ['search', 'albums'])
    assert different_deps['albums'] != ids['albums']
    assert different_deps['search'] == ids['search']


@pytest.mark.parametrize('deps', [{'albums': ['albums']}, {'albums': ['search'], 'search': ['albums']},
                                   {'albums': ['missing']}])
def test_invalid_prerequisite_graph_is_rejected_before_capture(ledger, deps):
    with pytest.raises(ValueError):
        compute_obligations('library', 'wave-1', 'frozen-1', ['search', 'albums'], prerequisite_scopes=deps)
    assert not ledger.table.rows


def test_capture_cannot_mix_library_revision_or_dependency_identities():
    table = EtagTable()
    ledger = ObligationLedger(table, 'wave-1', library='library-A', frozen_revision='frozen-1')
    wrong = compute_obligations('library-B', 'wave-1', 'frozen-1', ['search'])
    with pytest.raises(ValueError, match='declared wave'):
        ledger.record_required(wrong)
    ids = compute_obligations('library-A', 'wave-1', 'frozen-1', ['search'])
    ledger.record_required(ids)
    other = ObligationLedger(table, 'wave-1', library='library-B', frozen_revision='frozen-1')
    with pytest.raises(RuntimeError, match='identity mismatch'):
        other.completion_proof()


def test_capture_refuses_an_oversized_manifest_before_any_writes(ledger):
    scopes = ['scope' + str(i) for i in range(128)]
    deps = {scope: scopes[:i] for i, scope in enumerate(scopes)}
    ids = compute_obligations('lib', 'wave-1', 'rev-1', scopes, prerequisite_scopes=deps)
    with pytest.raises(ValueError, match='string-property limit'):
        ledger.record_required(ids, prerequisite_scopes=deps)
    assert not ledger.table.rows


def test_concurrent_different_captures_cannot_mix_required_sets(ledger, monkeypatch):
    first = compute_obligations('lib', 'wave-1', 'rev-1', ['search'])
    competing = compute_obligations('lib', 'wave-1', 'rev-1', ['people'])
    create = ledger.table.create_entity
    called = []
    def interleave(row):
        if row['RowKey'] == 'required-set-v2' and not called:
            called.append(True)
            ledger.record_required(competing)
        return create(row)
    monkeypatch.setattr(ledger.table, 'create_entity', interleave)
    with pytest.raises(RuntimeError, match='differs'):
        ledger.record_required(first)
    assert ledger.required_ids() == set(competing.values())
