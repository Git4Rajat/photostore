"""What a failed library clean tells the user."""
import automatic_index_updates as automatic
import app


def test_proof_lost_to_an_index_writer_gets_an_actionable_reason():
    reason = app._library_clean_failure_reason(automatic.SourceBusy('Live maintenance claim prevents quiesced recovery'))
    assert 'index update' in reason and 'resumes' in reason


def test_specific_refusals_keep_their_own_reason():
    assert 'Nothing was deleted' in app._library_clean_failure_reason(automatic.QuiescenceTimeout('busy'))
    assert 'administrator' in app._library_clean_failure_reason(automatic.OperatorFenced('fenced'))


def test_unknown_errors_stay_generic():
    assert app._library_clean_failure_reason(RuntimeError('boom')) == 'Library clean failed'
