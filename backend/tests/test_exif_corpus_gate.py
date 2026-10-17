import copy

import pytest

from scripts.audit_exif_corpus import bind_target


@pytest.fixture
def cohort(tmp_path):
    names = ['one.raw', 'two.raw']
    for name in names:
        (tmp_path / name).write_bytes(b'original')
    index = {'root': str(tmp_path), 'rows': [
        {'name': name, 'sourceBytes': 8, 'sourceSha256': 'a' * 64} for name in names]}
    rows = [{'name': name, 'sourceBytes': 8, 'extractable': True,
             'accepted': True, 'acceptedExact': True, 'originalFailure': None} for name in names]
    report = {'rows': rows, 'exiftoolVersion': '13.55', 'summary': {'acceptedMismatch': 0}}
    return index, report, copy.deepcopy(index), copy.deepcopy(report)


def test_target_binds_complete_fixed_cohort(cohort):
    summary = bind_target(*cohort)
    assert summary['target99Met'] is True
    assert summary['referenceEligible'] == 2
    assert summary['exactReusePercent'] == 100


@pytest.mark.parametrize('change', ['subset-index', 'subset-report', 'duplicate-index', 'duplicate-report',
    'source-changed', 'extra-file', 'version', 'report-size'])
def test_target_cannot_pass_by_selecting_a_smaller_or_changed_corpus(cohort, change):
    index, report, _, _ = cohort
    if change == 'subset-index':
        index['rows'].pop()
    elif change == 'subset-report':
        report['rows'].pop()
    elif change == 'duplicate-index':
        index['rows'].append(index['rows'][0])
    elif change == 'duplicate-report':
        report['rows'].append(report['rows'][0])
    elif change == 'source-changed':
        index['rows'][0]['sourceSha256'] = 'b' * 64
    elif change == 'extra-file':
        from pathlib import Path
        (Path(index['root']) / 'added.raw').touch()
    elif change == 'version':
        report['exiftoolVersion'] = '13.56'
    elif change == 'report-size':
        report['rows'][0]['sourceBytes'] = 9
    with pytest.raises(ValueError):
        bind_target(*cohort)


@pytest.mark.parametrize('category', ['unavailable', 'timeout', 'execution_failed', 'invalid_output', 'read_failed'])
def test_reference_failure_cannot_shrink_denominator(cohort, category):
    _, report, _, _ = cohort
    report['rows'][0].update(extractable=False, accepted=False, acceptedExact=False, originalFailure=category)
    summary = bind_target(*cohort)
    assert summary['referenceEligible'] == 2
    assert summary['exactReusePercent'] == 50
    assert summary['referenceExtractionRegressions'] == summary['infrastructureFailures'] == 1
    assert summary['target99Met'] is False


def test_target_recomputes_mismatches_from_rows_not_a_saved_counter(cohort):
    cohort[1]['rows'][0]['acceptedExact'] = False
    summary = bind_target(*cohort)
    assert summary['acceptedMismatch'] == 1
    assert summary['target99Met'] is False
