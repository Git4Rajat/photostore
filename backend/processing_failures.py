"""Bounded diagnostic capsules; never retain exception text or source payloads."""
import json
import os
import re

_LABEL = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_ERROR_CODE = re.compile(r'[a-z][a-z0-9_]{0,79}\Z')
STEPS = ('thumbnail', 'exif', 'ocr', 'face', 'ai_vision', 'map_detection')
STATUSES = {'pending', 'queued', 'running', 'done', 'no_data', 'failed', 'unrecoverable', 'skipped', 'unsupported'}


def label(value):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) else None


def error_code(value):
    return value if isinstance(value, str) and _ERROR_CODE.fullmatch(value) else None


def exception_details(error):
    """SDK status and exception class are useful without leaking signed URLs."""
    result = {'errorClass': type(error).__name__[:80]}
    status = getattr(error, 'status_code', None)
    if type(status) is int and 100 <= status <= 599:
        result['httpStatus'] = status
    cause = error.__cause__
    if cause is not None:
        result['causeClass'] = type(cause).__name__[:80]
    return result


def checkpoint_details(value):
    """Expose phase/count plus hashed cursor, never an opaque source key."""
    from upload_waves import digest
    result = dict(checkpointDigest=digest(value))
    if not isinstance(value, str) or ':' not in value:
        return result
    prefix, body = value.split(':', 1)
    if prefix in ('vision', 'index'):
        try:
            state = json.loads(body)
        except (ValueError, TypeError):
            return result
        if isinstance(state, dict):
            result['phase'] = label(state.get('phase'))
            if isinstance(state.get('after'), str):
                result['cursorDigest'] = digest(state['after'])
    elif prefix == 'faces-applied' and body.isdecimal() and len(body) <= 10:
        result.update(phase='assignment', processedFaces=int(body))
    elif prefix == 'published' and body in ('sort','lexical','vector','albums','people','tags','timeline','metadata'):
        result.update(phase='published', scope=body)
    return result


def source_details(row):
    try:
        metadata = json.loads(row.get('processing_metadata') or '{}')
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    statuses, errors = {}, {}
    for step in STEPS:
        status = row.get(step + '_status')
        if isinstance(status, str) and status in STATUSES:
            statuses[step] = status
        records = [metadata.get('client_' + step), metadata.get('client_' + step + '_rejected')]
        if step == 'exif':
            records.append(metadata.get('server_exif'))
        if status in ('failed', 'unrecoverable'):
            for result in records:
                if not isinstance(result, dict):
                    continue
                code = next((result.get(key) for key in ('errorCode', 'faceFailureStage', 'filteredReason', 'reason')
                             if error_code(result.get(key))), None)
                if code:
                    errors[step] = code
                    break
    return dict(statuses=statuses, errors=errors,
                generation=label(row.get('processingGeneration')))


def capsule(witness):
    """Allowlist the witness rather than storing arbitrary historical rows."""
    result = {'schemaVersion': 1, 'stage': 'vision'}
    if not isinstance(witness, dict):
        return result
    for key in ('attempts', 'checks', 'processedFaces'):
        value = witness.get(key)
        if type(value) is int and 0 <= value <= 1_000_000:
            result[key] = value
    for key in ('generation', 'error', 'errorClass', 'causeClass', 'checkpointDigest', 'cursorDigest', 'phase', 'scope'):
        if label(witness.get(key)):
            result[key] = witness[key]
    if type(witness.get('httpStatus')) is int and 100 <= witness['httpStatus'] <= 599:
        result['httpStatus'] = witness['httpStatus']
    for key in ('statuses', 'errors'):
        values = witness.get(key)
        if isinstance(values, dict):
            result[key] = {step: values[step] for step in STEPS
                           if (error_code(values.get(step)) if key == 'errors' else
                               isinstance(values.get(step),str) and values[step] in STATUSES)}
    return result


def deployment():
    return {key: value for key, value in (
        ('appRevision', label(os.getenv('CONTAINER_APP_REVISION'))),
        ('appRole', label(os.getenv('APP_ROLE')))) if value}
