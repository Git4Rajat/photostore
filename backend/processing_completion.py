"""Source-local completion intent committed with the authoritative metadata row.

This outbox is deliberately not a qualified wave receipt: the relay must also
validate immutable face/lookup output before advancing a production barrier.
Notifications can be lost; the row retains its intent until exact-ID ack.
"""
import hashlib
import json
import os

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError
from azure.data.tables import UpdateMode

STEPS = ('preview', 'thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face')
TERMINAL = {'done', 'no_data', 'skipped', 'unsupported'}
SCHEMA_VERSION = 4

# Distinct provenance for a terminal step outcome -- missing provenance,
# exhausted failures and unsupported/policy-excluded processing must not be
# collapsed into one undifferentiated "terminal" bucket at the completion
# layer. 'verified' is real model output; 'no_data' is a legitimate empty
# result (e.g. zero faces); 'policy_excluded' is skipped/unsupported by
# policy (e.g. every step on a video, which is browser-owned). There is
# deliberately no 'retry_exhausted' outcome yet: a step stuck at 'failed'
# is not in TERMINAL and blocks completion entirely -- the retry-exhaustion
# settlement path is still open (see the artifact-receipts audit's
# remaining release gates).
PROVENANCE = {'done': 'verified', 'no_data': 'no_data', 'skipped': 'policy_excluded', 'unsupported': 'policy_excluded'}


class StaleProcessingResult(RuntimeError):
    pass


def step_provenance(status):
    provenance = PROVENANCE.get(status)
    if provenance is None:
        raise ValueError(f'Status {status!r} has no completion provenance; it is not terminal')
    return provenance


def completion_capture_enabled():
    return os.getenv('UPLOAD_WAVE_SOURCE_COMPLETION_ENABLED', '').strip().lower() == 'true'


def check_revision(row, expected):
    if expected is not None and (not expected or row.get('processingRevision') != expected):
        raise StaleProcessingResult('Processing source revision changed.')


def completion_identity(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def validate_intent(intent, library):
    keys = {'schemaVersion', 'library', 'filename', 'photoId', 'revision', 'generation', 'attempt',
            'statuses', 'stepProvenance', 'faceCount', 'metadataDigest', 'derivatives', 'artifactReceipt'}
    if set(intent) - (keys | {'id', 'processingCompletionPending'}) or not keys <= set(intent):
        raise ValueError('Invalid source completion envelope')
    payload = {key: intent[key] for key in keys}
    if (type(payload['schemaVersion']) is not int or payload['schemaVersion'] != SCHEMA_VERSION or payload['library'] != library
            or any(not isinstance(payload[key], str) or not 0 < len(payload[key]) <= limit
                   for key, limit in [('filename', 1024), ('photoId', 256), ('revision', 128)])
            or not isinstance(payload['generation'], str) or len(payload['generation']) != 64
            or any(char not in '0123456789abcdef' for char in payload['generation'])
            or not isinstance(payload['attempt'], str) or len(payload['attempt']) > 128
            or not isinstance(payload['statuses'], dict) or set(payload['statuses']) != set(STEPS)
            or any(not isinstance(value, str) or value not in TERMINAL for value in payload['statuses'].values())
            or not isinstance(payload['stepProvenance'], dict) or set(payload['stepProvenance']) != set(STEPS)
            or any(payload['stepProvenance'][step] != step_provenance(payload['statuses'][step]) for step in STEPS)
            or (payload['faceCount'] is not None and
                (type(payload['faceCount']) is not int or payload['faceCount'] < 0))
            or not isinstance(payload['metadataDigest'], str) or len(payload['metadataDigest']) != 64
            or any(char not in '0123456789abcdef' for char in payload['metadataDigest'])
            or intent.get('id') != completion_identity(payload)):
        raise ValueError('Invalid source completion identity or scope')
    derivatives = payload['derivatives']
    if not isinstance(derivatives, dict) or set(derivatives) - {'thumbnail', 'preview'}:
        raise ValueError('Invalid derivative receipt set')
    from processing_artifacts import validate_receipt
    for kind, receipt in derivatives.items():
        if payload['statuses'][kind] != 'done':
            raise ValueError('Derivative receipt does not match required step outcome')
        validate_receipt(dict(PartitionKey=library, anonymousImageId=payload['photoId'],
            processingRevision=payload['revision'], **{kind + 'Artifact': json.dumps(receipt)}), kind)
    return payload


def prepare_completion(row):
    """Mutate a pending marker into the SAME row that its caller CAS commits.

    Failed/time-out steps remain blocked; the legacy processing_complete flag
    counts those as terminal but is insufficient evidence for wave readiness.
    """
    revision = row.get('processingRevision')
    generation = row.get('processingGeneration')
    statuses = {step: row.get(step + '_status') for step in STEPS}
    eligible = (bool(revision and generation and row.get('anonymousImageId'))
        and row.get('processing_state') != 'deleted'
        and not row.get('processingIsolated')
        and not any(row.get(key) for key in ('processing_lease_owner', 'processing_lease', 'processing_lease_expires_at'))
        and all(status in TERMINAL for status in statuses.values()))
    if not eligible:
        if revision or 'processingCompletionPending' in row:
            row['processingCompletionPending'] = ''
        return
    payload = dict(schemaVersion=SCHEMA_VERSION, library=row['PartitionKey'], filename=row['RowKey'],
        photoId=row['anonymousImageId'], revision=revision, generation=generation,
        stepProvenance={step: step_provenance(statuses[step]) for step in STEPS},
        attempt=row.get('processingFence') or '',
        statuses=statuses, faceCount=row.get('faceCount'),
        artifactReceipt=json.loads(row['artifactReceipt']) if row.get('artifactReceipt') else None,
        derivatives={kind: json.loads(row[kind + 'Artifact']) for kind in ('thumbnail', 'preview')
                     if statuses[kind] == 'done' and row.get(kind + 'Artifact')},
        metadataDigest=hashlib.sha256(json.dumps({key: row.get(key) for key in
            ('processing_metadata', 'thumbnailArtifact', 'previewArtifact', 'artifactReceipt')},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest())
    identity = completion_identity(payload)
    backlog = json.loads(row.get('processingCompletionOutbox') or '[]')
    if row.get('processingCompletionAck') != identity and not any(item['id'] == identity for item in backlog):
        # Preserve older generations until acknowledged. Capacity never evicts proof.
        if len(backlog) >= 16:
            raise RuntimeError('Source completion outbox requires recovery before another result commit')
        backlog.append(dict(payload, id=identity))
    encoded = json.dumps(backlog, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('utf-16-le')) > 60_000:
        raise RuntimeError('Source completion outbox byte capacity requires recovery before result commit')
    row['processingCompletionOutbox'] = encoded
    row['processingCompletionId'] = identity
    row['processingCompletionRevision'] = revision
    row['processingCompletionPending'] = '' if row.get('processingCompletionAck') == identity else identity


def relay_completion(table, library, filename, deliver):
    """Deliver at least once; acknowledge only the ID delivered, with its ETag.

    deliver MUST return the exact durable inbox record after idempotent acceptance.
    A lost response or a racing new completion leaves a recoverable pending row.
    """
    row = table.get_entity(library, filename)
    backlog = json.loads(row.get('processingCompletionOutbox') or '[]')
    if not backlog:
        return False
    intent = backlog[0]
    pending = intent['id']
    # The captured intent, not a fresh observation of another generation.
    accepted = deliver(dict(intent, processingCompletionPending=pending))
    if not isinstance(accepted, dict) or any(accepted.get(key) != value for key, value in {
            'completionId': pending, 'libraryId': library, 'photoId': intent['photoId'],
            'processingRevision': intent['revision']}.items()):
        return False  # No exact durable acceptance receipt, so retain the outbox.
    current = table.get_entity(library, filename)
    remaining = json.loads(current.get('processingCompletionOutbox') or '[]')
    if not any(item['id'] == pending for item in remaining):
        return False
    remaining = [item for item in remaining if item['id'] != pending]
    version = getattr(current, 'metadata', {}).get('etag')
    if not version:
        raise RuntimeError('Source completion acknowledgement requires an ETag')
    fields = dict(current, processingCompletionOutbox=json.dumps(remaining, sort_keys=True, separators=(',', ':')),
        processingCompletionAck=pending)
    if current.get('processingCompletionPending') == pending:
        fields['processingCompletionPending'] = ''
    try:
        table.update_entity(fields, etag=version, mode=UpdateMode.REPLACE,
            match_condition=MatchConditions.IfNotModified)
    except ResourceModifiedError:
        return False
    return True
