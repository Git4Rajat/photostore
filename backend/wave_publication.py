"""Exact obligation-ID publication receipts for a wave's required projections
-- see docs/indexing-clustering-target-state.md step 6 ("prove indexing
completion"). Reuses the same Azure Table CAS idioms as index_work.py/
clustering_wave.py rather than inventing new primitives.

Completion is exact-ID-set equality: the finite required obligation set is
persisted BEFORE publication starts (record_required), and is_complete()
checks every one of those exact IDs has a matching receipt -- never a
newer-timestamp/highest-revision/scope-only inference. A receipt for a
different wave or a different frozen input revision has a different
obligationId by construction (see compute_obligations) and can never
satisfy this wave's requirement.

No real publisher calls this yet -- see upload_wave_execution.py's and
clustering_wave.py's module docstrings on the equivalent scope limits for
steps 4 and 5. This is the next, independently tested layer.
"""
import hashlib
import json

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from index_work import etag

PARTITION_PREFIX = 'wave-obligations!'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def compute_obligations(library, wave_id, frozen_revision, required_scopes):
    """Returns {scope: obligationId} for this exact wave/revision/scope. A
    different wave, a different frozen_revision (e.g. a successor wave
    reusing a scope name), or a different scope each produce a distinct,
    unrelated obligationId -- never comparable or substitutable."""
    return {scope: digest({'library': library, 'waveId': wave_id,
                           'frozenRevision': frozen_revision, 'scope': scope})
            for scope in required_scopes}


class ObligationLedger:
    """One row per required obligation, partitioned by wave.

    record_required persists the finite set before publication starts;
    record_receipt marks one obligation satisfied (with its output/no-change
    evidence); is_complete checks the full captured set by exact ID
    equality, never by scope match, timestamp or a "latest" watermark.
    """

    def __init__(self, table, wave_id):
        self.table = table
        self.wave_id = wave_id
        self.partition = PARTITION_PREFIX + wave_id

    def record_required(self, obligations):
        """Persists the finite required set. Idempotent: re-recording the
        identical set is a no-op. Recording a DIFFERENT set for a wave that
        already has required obligations is rejected -- the required set is
        captured once, before publication starts, and must not grow or
        shrink afterward; late foreground work gets its own separate
        obligation set, never folded into this one.
        """
        existing = {row['RowKey'] for row in self._rows()}
        if existing:
            if existing != set(obligations.values()):
                raise RuntimeError('Required obligation set already captured and differs')
            return
        for scope, obligation_id in obligations.items():
            row = dict(PartitionKey=self.partition, RowKey=obligation_id, waveId=self.wave_id,
                       scope=scope, obligationId=obligation_id, status='pending', receipt='')
            try:
                self.table.create_entity(row)
            except ResourceExistsError:
                pass  # Concurrent identical capture; same content either way.

    def record_receipt(self, obligation_id, *, output_generation=None, no_change=False):
        """Conditionally commits a receipt for an exact obligationId. A
        receipt for an obligationId never recorded by record_required (a
        foreign wave, a stale/foreign scope, or a receipt sent before the
        required set existed) is rejected -- it cannot satisfy a
        requirement that was never asked for. An explicit no-change receipt
        (nothing needed publishing for this scope) still counts as
        satisfied -- empty projection obligations require no fake artifact.
        """
        for _ in range(12):
            try:
                row = self.table.get_entity(self.partition, obligation_id)
            except ResourceNotFoundError:
                raise RuntimeError('Receipt for an obligation that was never required')
            if row.get('status') == 'satisfied':
                return  # Idempotent: duplicate dispatch/receipt.
            updated = dict(row, status='satisfied', receipt=json.dumps(
                {'outputGeneration': output_generation, 'noChange': bool(no_change)}))
            try:
                self.table.update_entity(updated, etag=etag(row))
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Obligation receipt contention')

    def required_ids(self):
        return {row['RowKey'] for row in self._rows()}

    def is_complete(self):
        rows = list(self._rows())
        return bool(rows) and all(row.get('status') == 'satisfied' for row in rows)

    def _rows(self):
        return self.table.query_entities(f"PartitionKey eq '{self.partition}'")
