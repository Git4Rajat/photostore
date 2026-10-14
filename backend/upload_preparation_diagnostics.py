"""Bounded, non-authoritative browser claims plus backend receipt evidence."""
import json
import math

ASSETS = ('preview', 'thumbnail', 'metadata')
REASONS = frozenset({'disposed', 'caller_aborted', 'unsupported_type', 'busy', 'missing_target',
                    'pipeline_timeout', 'internal_error', 'generation_failed', 'timeout', 'aborted',
                    'unsupported', 'unsupported_raw', 'resource_limit', 'metadata_budget',
                    'metadata_unsupported', 'metadata_reference', 'metadata_structure',
                    'invalid_derivative', 'worker_error', 'worker_start_failed', 'http_error', 'network_error',
                    'packet_invalid', 'backend_disabled', 'unqualified_family', 'not_requested', 'missing_binding', 'target_unavailable',
                    'generation_timeout', 'generation_queue_timeout', 'upload_timeout',
                    'video_decode_unsupported', 'video_metadata_unsupported', 'metadata_read_budget'})


def metadata_offer_reason(requested, physical, filename, url):
    from upload_metadata import uploads_enabled, qualified_family
    if not requested:
        return 'not_requested'
    if not uploads_enabled(filename):
        return 'backend_disabled'
    if not physical:
        return 'missing_binding'
    if not qualified_family(filename):
        return 'unqualified_family'
    return None if url else 'target_unavailable'


def bounded_report(value):
    from upload_metadata import MAX_PACKET_BYTES
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        return None
    result = {'version': 1, 'assets': {}}
    if type(value.get('reason')) is str and value['reason'] in REASONS:
        result['reason'] = value['reason']
    for field in ('elapsedMs', 'admissionMs'):
        number = value.get(field)
        if type(number) in (int, float) and 0 <= number <= 3_600_000 and math.isfinite(number):
            result[field] = round(number)
    for asset in ASSETS:
        raw = (value.get('assets') or {}).get(asset) if isinstance(value.get('assets'), dict) else None
        if not isinstance(raw, dict):
            continue
        clean = {}
        for field, allowed in [('generation', {'not_attempted', 'generated', 'failed', 'skipped'}),
                               ('upload', {'not_attempted', 'uploaded', 'failed', 'aborted'})]:
            if type(raw.get(field)) is str and raw[field] in allowed:
                clean[field] = raw[field]
        if type(raw.get('target')) is bool:
            clean['target'] = raw['target']
        if type(raw.get('runtime')) is str and raw['runtime'] in {'embedded_video_jpeg', 'native_video', 'ffmpeg_wasm', 'mediainfo_wasm'}:
            clean['runtime'] = raw['runtime']
        for field in ('generationReason', 'uploadReason'):
            if type(raw.get(field)) is str and raw[field] in REASONS:
                clean[field] = raw[field]
        for field, cap in [('generationMs', 3_600_000), ('uploadMs', 3_600_000),
                           ('queueMs', 3_600_000), ('bytes', MAX_PACKET_BYTES if asset == 'metadata' else 4_194_304),
                           ('httpStatus', 599), ('attempts', 3), ('readBytes', 32 * 1024 * 1024), ('readCalls', 2048)]:
            number = raw.get(field)
            if type(number) in (int, float) and 0 <= number <= cap and math.isfinite(number):
                clean[field] = round(number)
        result['assets'][asset] = clean
    return result


def rejection_reason(error, domain='metadata'):
    if isinstance(error, ValueError):
        message = str(error)
        if message.startswith('Direct derivative'):
            if 'byte limit' in message:
                return 'derivative_byte_limit'
            if 'content type' in message:
                return 'derivative_content_type'
            if 'changed' in message:
                return 'derivative_changed'
            return 'derivative_format_or_dimensions'
        if message == 'Derivative storage is unavailable':
            return 'derivative_storage_unavailable'
        if 'mismatch' in message.lower():
            return 'binding_or_blob_mismatch'
        if message.startswith('Invalid metadata'):
            return 'invalid_descriptor'
        return 'invalid_' + domain
    status = getattr(error, 'status_code', None)
    return 'storage_http_' + str(status) if type(status) is int and 400 <= status <= 599 else domain + '_exception'


def record_receipt(app, user_id, filename, payload, *, packet_accepted=False, packet_rejection=None, processing_rejection=None, duplicate=False):
    report = bounded_report(payload.get('preparationReport'))
    # Do not add storage work or change behavior for old/non-preparation clients.
    if report is None:
        return
    try:
        row = app.metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
        processing = json.loads(row.get('processing_metadata') or '{}')
        claims = payload.get('clientProcessing') or {}
        client_asset_id = str(payload.get('clientAssetId') or payload.get('uploadId') or '')
        receipts = {}
        for asset in ('preview', 'thumbnail'):
            submitted = isinstance(claims.get(asset), dict) and claims[asset].get('alreadyUploaded') is True
            source = (processing.get('client_' + asset) or {}).get('source')
            rejection = processing.get('client_' + asset + '_rejected')
            rejected_this_upload = isinstance(rejection, dict) and rejection.get('clientAssetId') == client_asset_id
            receipts[asset] = ('duplicate' if duplicate else 'accepted' if submitted
                               and not rejected_this_upload and row.get(asset + '_status') == 'done' and source == 'browser'
                               else 'rejected' if submitted else 'not_submitted')
            if receipts[asset] == 'rejected' and rejected_this_upload:
                receipts[asset + 'Reason'] = str(rejection.get('reason') or 'invalid_derivative')[:64]
        receipts['metadata'] = ('duplicate' if duplicate else 'accepted' if packet_accepted
                                else 'rejected' if payload.get('clientMetadata') else 'not_submitted')
        if packet_rejection and payload.get('clientMetadata'):
            receipts['metadataReason'] = packet_rejection
        if processing_rejection and any(receipts[asset] == 'rejected' for asset in ('preview', 'thumbnail')):
            receipts['processingReason'] = processing_rejection
        record = {'browserClaim': report, 'backendReceipt': receipts}
        app._update_metadata_entity_fields(user_id, filename, {'uploadPreparationReport': json.dumps(record, separators=(',', ':'))})
        app.app.logger.info('upload preparation receipt %s', json.dumps({
            'user': user_id, 'file': filename, 'uploadId': str(payload.get('uploadId') or '')[:100], **record}, separators=(',', ':')))
    except Exception:
        # Diagnostic failures never reject a successfully uploaded original.
        app.app.logger.info('upload preparation receipt unavailable user=%s file=%s', user_id, filename)
