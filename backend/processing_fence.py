"""Attempt-scoped CAS protection for source-owned face output.

A source check alone has a check/write race. Read the artifact version first,
then check the source attempt, and condition its write on that artifact version.
Never retry a rejected write against a newer artifact version.
"""
import contextvars
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import UpdateMode

from processing_completion import StaleProcessingResult
import processing_metrics

_ACTIVE = contextvars.ContextVar('processing_result_fence', default=None)
_PUBLISHED = contextvars.ContextVar('attempt_verified_derivatives', default=None)
_IMMUTABLE_CHECKS = contextvars.ContextVar('immutable_artifact_source_checks', default=None)
POLICY_CHECK = None


@dataclass(frozen=True)
class ResultFence:
    table: object
    library: str
    filename: str
    revision: str
    attempt: str
    owner: str
    photo: str
    # Optional for backward compatibility with existing positional callers;
    # empty means "not checked" (pre-generation rows), never a wildcard
    # match against a row that DOES carry one -- see check() below.
    generation: str = ''

    def check(self):
        if POLICY_CHECK is not None:
            POLICY_CHECK(self.library)
        row = self.table.get_entity(self.library, self.filename)
        try:
            expires = datetime.fromisoformat(str(row.get('processing_lease_expires_at') or '').replace('Z', '+00:00'))
            valid_time = expires > datetime.now(timezone.utc)
        except (TypeError, ValueError):
            valid_time = False
        if (not self.photo or row.get('processingRevision') != self.revision
                or row.get('processingFence') != self.attempt
                or row.get('processing_lease_owner') != self.owner
                or row.get('anonymousImageId') != self.photo
                or (self.generation and row.get('processingGeneration') != self.generation)
                or row.get('processing_state') == 'deleted' or row.get('processingIsolated')
                or not valid_time):
            raise StaleProcessingResult('Processing artifact attempt is no longer owned.')

    def headers(self):
        fields = dict(processingRevision=self.revision, processingFence=self.attempt, sourcePhotoId=self.photo)
        if self.generation:
            fields['processingGeneration'] = self.generation
        return fields


@contextmanager
def result_fence(fence):
    token = _ACTIVE.set(fence)
    published = _PUBLISHED.set({})
    try:
        yield
    finally:
        _PUBLISHED.reset(published)
        _ACTIVE.reset(token)


def current_fence(library=None, filename=None):
    fence = _ACTIVE.get()
    if fence and ((library is not None and library != fence.library)
                  or (filename is not None and filename != fence.filename)):
        raise StaleProcessingResult('Processing artifact scope mismatch.')
    return fence


def check_immutable_source(fence):
    """Only immutable blob/receipt producers may defer their source check."""
    batch = _IMMUTABLE_CHECKS.get()
    if batch is not None and batch['fence'] is fence and current_fence() is fence:
        batch['checks'] += 1
        return
    processing_metrics.add(artifact_source_checks=1)
    fence.check()


@contextmanager
def immutable_artifact_checks(fence):
    """One final source check before immutable results reach the metadata CAS.

    This scope must contain only create-only blobs/receipts and local metadata
    edits. Mutable face writes keep their immediate ResultFence.check checks.
    Failure leaves only unreferenced artifacts; no source pointer is committed.
    """
    if fence is None or current_fence() is not fence:
        raise StaleProcessingResult('Immutable artifact batch requires its active source fence.')
    token = _IMMUTABLE_CHECKS.set(dict(fence=fence, checks=0))
    try:
        yield
        checks = _IMMUTABLE_CHECKS.get()['checks']
        if checks:
            import processing_diagnostics
            with processing_diagnostics.apply_phase(fence.library, fence.filename, 'artifact_source_validation'):
                processing_metrics.add(artifact_source_checks=1, artifact_source_check_batches=1,
                                       artifact_source_checks_coalesced=max(0, checks - 1))
                fence.check()
    finally:
        _IMMUTABLE_CHECKS.reset(token)


@processing_metrics.purpose('artifact_write')
def write_artifact(table, entity, *, previous=None, previous_known=False):
    fence = current_fence(entity['PartitionKey'], entity.get('filename'))
    if fence is None:
        table.upsert_entity(entity)
        return
    from face_write_batch import enqueue
    if enqueue(table, entity, fence, previous, previous_known):
        return
    if not previous_known:
        try:
            previous = table.get_entity(entity['PartitionKey'], entity['RowKey'])
        except ResourceNotFoundError:
            previous = None
    # A reread after this check must never upgrade the write's artifact ETag.
    fence.check()
    row = dict(entity, **fence.headers())
    if previous is None:
        table.create_entity(row)
    else:
        version = getattr(previous, 'metadata', {}).get('etag')
        if not version or version == '*':
            raise StaleProcessingResult('Processing artifact requires a genuine ETag.')
        table.update_entity(row, mode=UpdateMode.REPLACE, etag=version,
                            match_condition=MatchConditions.IfNotModified)


def remember_published_derivative(receipt):
    """Remember immutable publication proven by read-back or validated upload ACK."""
    fence, verified = _ACTIVE.get(), _PUBLISHED.get()
    if (fence is not None and verified is not None
            and receipt.get('library') == fence.library and receipt.get('photoId') == fence.photo
            and receipt.get('revision') == fence.revision and receipt.get('attempt') == fence.attempt):
        verified[receipt['kind']] = json.dumps(receipt, sort_keys=True, separators=(',', ':'))


def published_derivative_matches(receipt):
    verified = _PUBLISHED.get()
    return (verified is not None and verified.get(receipt.get('kind'))
            == json.dumps(receipt, sort_keys=True, separators=(',', ':')))
