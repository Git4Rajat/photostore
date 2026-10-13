import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from upload_preparation_diagnostics import bounded_report, metadata_offer_reason, record_receipt, rejection_reason


def report():
    return {'version': 1, 'elapsedMs': 12.5, 'assets': {
        asset: {'target': True, 'generation': 'generated', 'upload': 'uploaded', 'bytes': 100}
        for asset in ('preview', 'thumbnail', 'metadata')}}


def test_report_drops_untrusted_strings_and_unbounded_values():
    raw = report()
    raw.update(reason='secret url', elapsedMs=float('inf'), password='secret')
    raw['assets']['preview'].update(generationReason='secret', uploadReason={'secret': True},
                                  bytes=4_194_305, queueMs=True, httpStatus=600, uploadMs=float('nan'))
    clean = bounded_report(raw)
    assert set(clean) == {'version', 'assets'}
    assert clean['assets']['preview'] == {'target': True, 'generation': 'generated', 'upload': 'uploaded'}
    assert 'secret' not in json.dumps(clean)


@pytest.mark.parametrize('value', [None, [], {'version': True}, {'version': 2}, 'secret'])
def test_invalid_report_has_no_storage_side_effects(value):
    app = Mock()
    record_receipt(app, 'library', 'photo.cr3', {'preparationReport': value})
    assert not app.mock_calls


def test_unhashable_enum_inputs_are_ignored():
    assert bounded_report({'version': 1, 'reason': [], 'assets': {'preview': {
        'generation': {}, 'upload': [], 'generationReason': {}, 'target': 'true'}}}) == {'version': 1, 'assets': {'preview': {}}}
    assert bounded_report({'version': 1, 'elapsedMs': 10 ** 1000, 'assets': {
        'preview': {'bytes': 10 ** 1000}}}) == {'version': 1, 'assets': {'preview': {}}}


def test_receipt_uses_backend_state_not_browser_success_claim():
    row = {'preview_status': 'done', 'thumbnail_status': 'pending',
           'processing_metadata': json.dumps({'client_preview': {'source': 'browser'}})}
    app = SimpleNamespace(metadata_table_client=SimpleNamespace(get_entity=lambda **_: row),
                          _update_metadata_entity_fields=Mock(), app=SimpleNamespace(logger=Mock()))
    payload = {'preparationReport': report(), 'clientProcessing': {
        'preview': {'alreadyUploaded': True}, 'thumbnail': {'alreadyUploaded': True}}, 'clientMetadata': {'version': 1}}
    record_receipt(app, 'library', 'photo.cr3', payload, packet_rejection='binding_or_blob_mismatch',
                   processing_rejection='storage_http_404')
    record = json.loads(app._update_metadata_entity_fields.call_args.args[2]['uploadPreparationReport'])
    assert record['backendReceipt'] == {'preview': 'accepted', 'thumbnail': 'rejected',
        'metadata': 'rejected', 'metadataReason': 'binding_or_blob_mismatch', 'processingReason': 'storage_http_404'}
    assert record['browserClaim']['assets']['thumbnail']['upload'] == 'uploaded'
    record_receipt(app, 'library', 'photo.cr3', payload, packet_accepted=True)
    accepted = json.loads(app._update_metadata_entity_fields.call_args.args[2]['uploadPreparationReport'])
    assert accepted['backendReceipt']['metadata'] == 'accepted'
    record_receipt(app, 'library', 'photo.cr3', payload, duplicate=True)
    duplicate = json.loads(app._update_metadata_entity_fields.call_args.args[2]['uploadPreparationReport'])
    assert set(duplicate['backendReceipt'].values()) == {'duplicate'}


def test_receipt_failure_does_not_reject_original():
    app = SimpleNamespace(metadata_table_client=SimpleNamespace(get_entity=Mock(side_effect=RuntimeError('offline'))),
                          _update_metadata_entity_fields=Mock(), app=SimpleNamespace(logger=Mock()))
    record_receipt(app, 'library', 'photo.cr3', {'preparationReport': report()})
    app._update_metadata_entity_fields.assert_not_called()
    app.app.logger.info.assert_called_once()


def test_offer_reports_gates_without_expanding_formats(monkeypatch):
    monkeypatch.setenv('BROWSER_UPLOAD_PREPARATION', 'false')
    assert metadata_offer_reason(True, 'id', 'photo.cr3', None) == 'backend_disabled'
    monkeypatch.setenv('BROWSER_UPLOAD_PREPARATION', 'true')
    assert metadata_offer_reason(False, 'id', 'photo.cr3', None) == 'not_requested'
    assert metadata_offer_reason(True, None, 'photo.cr3', None) == 'missing_binding'
    assert metadata_offer_reason(True, 'id', 'photo.orf', None) == 'unqualified_family'
    assert metadata_offer_reason(True, 'id', 'photo.cr3', None) == 'target_unavailable'
    assert metadata_offer_reason(True, 'id', 'photo.cr3', 'url') is None


@pytest.mark.parametrize('message,reason', [
    ('Direct derivative exceeds byte limit or is empty', 'derivative_byte_limit'),
    ('Direct derivative must have image/jpeg content type', 'derivative_content_type'),
    ('Direct derivative changed during verification', 'derivative_changed'),
    ('Direct derivative exceeds pixel limit or is not JPEG', 'derivative_format_or_dimensions'),
    ('Metadata source mismatch', 'binding_or_blob_mismatch'),
])
def test_rejection_codes_do_not_forward_exception_text(message, reason):
    assert rejection_reason(ValueError(message)) == reason
