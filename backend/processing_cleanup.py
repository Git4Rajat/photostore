"""Generation-aware cleanup for the processing-artifact namespaces: staging/
published derivative blobs and immutable face-snapshot/manifest rows -- see
docs/indexing-clustering-target-state.md step 4 ("cleanup integration").

These are bounded, explicit utility functions for an operator/library-
cleanup job to call. Nothing here runs automatically and nothing here is
wired into app.py's permanent-deletion entry points yet (_hard_delete_
photos_chunk, _execute_library_clean, _purge_library_data) -- see the
upload-artifact-receipts audit's remaining release gates for the precise
integration points still open there: preserving artifacts referenced by an
active/retryable wave, and fencing active writers during cleanup so a late
worker cannot recreate data after deletion. Deletion is irreversible; every
function here requires its caller to have already established that its
target is unreferenced, not infer that itself from a single read.
"""
import json

from azure.core.exceptions import ResourceNotFoundError

from processing_artifacts import staging_name
from upload_waves import digest


def delete_staging_attempt(storage, library, photo, revision, attempt, *, current_attempt):
    """Deletes one SPECIFIC abandoned attempt's staging blobs (thumbnail and
    preview). Refuses to delete the row's currently active attempt -- the
    caller must pass the row's live processingFence as current_attempt, not
    just assume whatever attempt it holds is stale."""
    if attempt == current_attempt:
        raise ValueError('Cannot delete staging for the currently active processing attempt')
    container = storage._CTX.get('blob_thumbnail_container')
    if not container:
        return 0
    deleted = 0
    for kind in ('thumbnail', 'preview'):
        name = staging_name(library, photo, revision, attempt, kind)
        blob = storage._get_blob_client(container, name)
        if blob is None:
            continue
        try:
            blob.delete_blob()
            deleted += 1
        except ResourceNotFoundError:
            pass
    return deleted


def _parse_json(raw, description):
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        # Fail closed: corrupt/unparseable completion evidence must block
        # cleanup, not be silently skipped and undercount what a row/inbox
        # entry still references -- an undercount is exactly what would
        # make a still-needed manifest look orphaned.
        raise ValueError(f'Corrupt or unreadable {description}') from exc


def referenced_manifest_ids(metadata_rows, inbox_rows=()):
    """Given an iterable of live metadata rows and an iterable of durable
    source-completion inbox rows (upload_waves.ShadowWaves's
    `source-completion!...` partition -- pass every row for the library, not
    a sample), returns the set of artifact manifest IDs still referenced via
    a row's `processingCompletionOutbox` backlog, its committed
    `artifactReceipt` pointer, or an inbox entry's own `artifactReceipt`.

    Deliberately does NOT treat `processingCompletionAck` as a manifest
    reference -- it is a completion-INTENT identity (processing_completion.
    completion_identity), a different identity space entirely; treating it
    as one was a bug (it happened to never collide with a real manifest ID,
    but asserted a reference that doesn't exist).

    Callers must survey every live metadata row and every inbox row for the
    library, not a sample, before treating anything outside this set as
    orphaned -- this function only aggregates what it is given. Raises on
    any entry whose completion evidence cannot be parsed, rather than
    silently skipping it (see `_parse_json`).
    """
    referenced = set()
    for row in metadata_rows:
        backlog = row.get('processingCompletionOutbox')
        if backlog:
            for item in _parse_json(backlog, f"processingCompletionOutbox for {row.get('RowKey')!r}"):
                receipt = item.get('artifactReceipt') if isinstance(item, dict) else None
                if isinstance(receipt, dict) and isinstance(receipt.get('id'), str):
                    referenced.add(receipt['id'])
        receipt_raw = row.get('artifactReceipt')
        if receipt_raw:
            parsed = _parse_json(receipt_raw, f"artifactReceipt for {row.get('RowKey')!r}")
            if isinstance(parsed, dict) and isinstance(parsed.get('id'), str):
                referenced.add(parsed['id'])
    for row in inbox_rows:
        payload_raw = row.get('payload')
        if not payload_raw:
            continue
        payload = _parse_json(payload_raw, f"source-completion inbox payload for {row.get('RowKey')!r}")
        receipt = payload.get('artifactReceipt') if isinstance(payload, dict) else None
        if isinstance(receipt, dict) and isinstance(receipt.get('id'), str):
            referenced.add(receipt['id'])
    return referenced


def _manifest_rows(table, library):
    partition = 'processing-artifacts-' + digest(library)
    for row in table.query_entities(f"PartitionKey eq '{partition}'", results_per_page=1000):
        if row['RowKey'].startswith('manifest!'):
            yield row


def orphaned_manifest_candidates(table, library, referenced_ids):
    """Read-only scan of this library's artifact partition for manifest rows
    not in `referenced_ids`. Returns candidates for the caller to delete
    only after it has independently confirmed `referenced_ids` reflects
    every live metadata row AND every inbox row -- a stale or partial
    survey here would make a still-needed manifest look orphaned.
    """
    return [row for row in _manifest_rows(table, library)
            if row['RowKey'][len('manifest!'):] not in referenced_ids]


def retained_manifest_rows(table, library, referenced_ids):
    """The complement of `orphaned_manifest_candidates`: manifest rows that
    ARE still referenced. Feed these into `protected_artifacts` to compute
    the blob/snapshot sets `delete_orphaned_manifest` must never delete,
    even when they also happen to appear inside an orphaned manifest.
    """
    return [row for row in _manifest_rows(table, library)
            if row['RowKey'][len('manifest!'):] in referenced_ids]


def protected_artifacts(retained_rows):
    """Aggregates every derivative blob name and face-snapshot ID referenced
    by manifests the caller has confirmed are still live (`retained_manifest_
    rows`). A derivative blob is content-addressed by its bytes, not by
    which manifest captured it -- two distinct manifests (different
    generations, different face snapshots) can reference the identical
    blob if a recapture produced byte-identical thumbnail/preview output.
    Deleting an orphaned manifest must never delete a blob or snapshot that
    a RETAINED manifest also points to.
    """
    blobs, snapshots = set(), set()
    for row in retained_rows:
        payload = _parse_json(row['payload'], f"manifest payload for {row.get('RowKey')!r}")
        blobs.update(receipt['blob'] for receipt in payload.get('derivatives', {}).values())
        snapshots.update(payload.get('faceSnapshots', []))
    return blobs, snapshots


def delete_orphaned_manifest(table, storage, manifest_row, *, protected_blobs=frozenset(), protected_snapshots=frozenset()):
    """Permanently deletes one confirmed-orphaned manifest: its published
    derivative blobs (except any in `protected_blobs`), then its face
    snapshot rows (except any in `protected_snapshots`), then the manifest
    row itself -- blobs first, so a crash after partial deletion leaves the
    manifest row as evidence of what's still outstanding rather than
    orphaning blobs with no remaining record of them.

    The caller must have already confirmed this manifest is not in the
    referenced set (orphaned_manifest_candidates) and computed
    `protected_blobs`/`protected_snapshots` from every RETAINED manifest
    (protected_artifacts(retained_manifest_rows(...))); this function
    trusts those decisions rather than re-deriving them.
    """
    payload = _parse_json(manifest_row['payload'], f"manifest payload for {manifest_row.get('RowKey')!r}")
    container = storage._CTX.get('blob_thumbnail_container')
    deleted_blobs = skipped_blobs = 0
    for receipt in payload.get('derivatives', {}).values():
        if receipt['blob'] in protected_blobs:
            skipped_blobs += 1
            continue
        if not container:
            continue
        blob = storage._get_blob_client(container, receipt['blob'])
        if blob is None:
            continue
        try:
            blob.delete_blob()
            deleted_blobs += 1
        except ResourceNotFoundError:
            pass
    deleted_snapshots = skipped_snapshots = 0
    for snapshot_id in payload.get('faceSnapshots', []):
        if snapshot_id in protected_snapshots:
            skipped_snapshots += 1
            continue
        # Explicit existence check before delete: some table fakes (and not
        # every real backend) raise on deleting an absent key, so this must
        # not rely on that to decide whether a deletion actually happened.
        try:
            table.get_entity(manifest_row['PartitionKey'], 'face!' + snapshot_id)
        except ResourceNotFoundError:
            continue
        table.delete_entity(manifest_row['PartitionKey'], 'face!' + snapshot_id)
        deleted_snapshots += 1
    table.delete_entity(manifest_row['PartitionKey'], manifest_row['RowKey'])
    return dict(deletedBlobs=deleted_blobs, deletedSnapshots=deleted_snapshots,
                skippedBlobs=skipped_blobs, skippedSnapshots=skipped_snapshots)
