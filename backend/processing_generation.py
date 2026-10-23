"""Frozen processing generation: photo/source identity bound to the required-
step and model/taxonomy/policy contract in force when it was allocated.

A processingRevision alone (storage_utils.finalize_uploaded_file) is an
opaque identity token: it proves a row hasn't been silently replaced, but
says nothing about WHICH photo/source bytes, required steps, or model
versions were in force when it was allocated. This module freezes all of
that into one content-addressed contract ID at the same moment the
revision itself is allocated, and detects when either the source or the
live model/policy contract has since moved past it, so a stale generation
can be explicitly superseded instead of silently assumed current.

A new generation is allocated only for a changed source (photo identity or
source-byte hash), a changed contract (model/taxonomy/policy version bump
or a required-step-set change), or explicit forced reprocessing -- never
for an ordinary lease renewal/retry/receipt-recovery call, which must
retain the existing generation and only mint a fresh attempt token (see
storage_utils.claim_processing_lease's processingFence handling).

Reads the same model-version environment variables app.py already defines
(duplicated rather than imported, matching
IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION's own existing duplication
rationale there: these leaf modules must stay importable without pulling in
app.py's full dependency graph).
"""
import hashlib
import json
import os

# Image media gets the full backend-processed step set. Video is browser-
# owned at finalize (storage_utils.finalize_uploaded_file's
# video_status_overrides immediately settles preview/ocr/ai_vision/face as
# 'skipped' and thumbnail/exif as 'failed') -- its processing contract
# requires none of these backend steps, not the same seven as an image.
REQUIRED_STEPS = ('preview', 'thumbnail', 'exif', 'ocr', 'ai_vision', 'map_detection', 'face')

# Bumped when a required-step/processing-policy change isn't captured by a
# model-version env var on its own (e.g. a new required step, a changed
# eligibility rule) -- gives operators an explicit lever to force every
# future allocation onto a new contract without needing a fake model bump.
PROCESSING_POLICY_VERSION = os.getenv('PROCESSING_POLICY_VERSION', '1').strip()


def required_steps_for_media(is_video):
    return () if is_video else REQUIRED_STEPS


def _face_embedding_allowed_versions():
    return {version for version in {
        os.getenv('FACE_CLUSTER_EMBEDDING_VERSION', '').strip(),
        os.getenv('IPWORKER_FACE_CLUSTER_EMBEDDING_VERSION',
                   'adaface-ir101-webface4m-512d-v1+mediapipe-landmark-478').strip(),
    } if version}


def current_contract():
    """The live model/taxonomy/policy half of the contract, independent of
    any one photo -- the part that moves when an operator bumps a version
    env var, rather than when a specific photo's source bytes change."""
    return {
        'faceEmbeddingVersions': sorted(_face_embedding_allowed_versions()),
        'policyVersion': PROCESSING_POLICY_VERSION,
    }


def build_contract(*, photo_id, file_hash, is_video):
    if not photo_id or not file_hash:
        raise ValueError('A processing contract requires photo and source-byte identity')
    return {
        'photoId': photo_id,
        'sourceByteHash': file_hash,
        'requiredSteps': sorted(required_steps_for_media(bool(is_video))),
        **current_contract(),
    }


def contract_id(contract):
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def allocate_generation(revision, *, photo_id, file_hash, is_video):
    """Freezes the live contract, bound to this exact photo/source, into a
    generation descriptor for a newly allocated processingRevision. Called
    once, alongside the revision stamp itself -- immutable thereafter,
    exactly like the revision it travels with."""
    if not revision:
        raise ValueError('Generation allocation requires a processing revision')
    contract = build_contract(photo_id=photo_id, file_hash=file_hash, is_video=is_video)
    return {
        'processingRevision': revision,
        'processingGeneration': contract_id(contract),
        'processingContract': json.dumps(contract, sort_keys=True, separators=(',', ':')),
    }


def is_superseded(row, *, is_video):
    """True once this row's frozen generation no longer matches what a fresh
    allocation for its OWN recorded photo/source would produce under the
    live model/policy contract -- i.e. either the source changed without a
    new generation ever being allocated (a bug elsewhere, since finalize
    always reallocates on source change), or an operator has since bumped a
    required model/taxonomy/embedding/policy version.

    A row with no recorded generation at all (pre-migration, or a legacy
    writer that never called allocate_generation) is ALSO treated as
    superseded: it must go through explicit qualification/migration, never
    be assumed current by omission.
    """
    # Ordinary (non-anonymized) rows have no anonymousImageId; the row's own
    # identity (filename/RowKey) is the physical photo identity then --
    # same fallback storage_utils.py uses throughout (e.g. source_blob).
    photo_id = str(row.get('anonymousImageId') or '').strip() or row.get('RowKey')
    file_hash = row.get('fileHash')
    if not photo_id or not file_hash:
        return True
    contract = build_contract(photo_id=photo_id, file_hash=file_hash, is_video=is_video)
    return row.get('processingGeneration') != contract_id(contract)


def supersede(row, new_revision, *, is_video):
    """Returns the frozen fields an explicit forced reprocess must apply: a
    fresh revision/generation pair bound to the row's current photo/source,
    and every required step for its media type reset to pending.

    Does not mutate `row`, perform the reset itself, apply any CAS/lease
    protocol, or touch model-owned processing_metadata provenance -- the
    caller commits these fields alongside whatever durable write path it
    already uses. This function only computes what a correct supersession
    must contain, distinguishing it from an ordinary retry: a retry must
    never call this and must never receive a new generation.
    """
    if not new_revision:
        raise ValueError('Supersession requires a freshly allocated revision')
    if new_revision == row.get('processingRevision'):
        raise ValueError('Supersession requires a revision distinct from the current one')
    photo_id = str(row.get('anonymousImageId') or '').strip() or row.get('RowKey')
    fields = allocate_generation(new_revision, photo_id=photo_id,
                                 file_hash=row.get('fileHash'), is_video=is_video)
    for step in required_steps_for_media(bool(is_video)):
        fields[step + '_status'] = 'pending'
    return fields
