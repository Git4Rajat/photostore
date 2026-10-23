"""Immutable projection obligations and exact publication receipts.

A create-once manifest captures the ENTIRE required set before any obligation
rows are materialized. Partial capture is repairable; partial receipts cannot
prove completion. No live publisher is connected to this protocol yet.
"""
import hashlib
import json
import re

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode

from index_work import etag

PARTITION_PREFIX = 'wave-obligations!'
MANIFEST_KEY = 'required-set-v2'
MAX_OBLIGATIONS = 128


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _graph(scopes, prerequisite_scopes):
    scopes = list(scopes)
    if (len(scopes) > MAX_OBLIGATIONS or len(set(scopes)) != len(scopes)
            or any(not isinstance(scope, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', scope)
                   for scope in scopes)):
        raise ValueError('Invalid required scopes')
    graph = {scope: sorted(set((prerequisite_scopes or {}).get(scope, ()))) for scope in scopes}
    if set(prerequisite_scopes or {}) - set(scopes) or any(set(deps) - set(scopes) for deps in graph.values()):
        raise ValueError('Prerequisites must belong to the captured obligation set')
    visited, visiting = set(), set()
    def visit(scope):
        if scope in visiting:
            raise ValueError('Cyclic publication prerequisites')
        if scope in visited:
            return
        visiting.add(scope)
        for parent in graph[scope]:
            visit(parent)
        visiting.remove(scope)
        visited.add(scope)
    for scope in scopes:
        visit(scope)
    return graph


def compute_obligations(library, wave_id, frozen_revision, required_scopes, *, prerequisite_scopes=None):
    graph = _graph(required_scopes, prerequisite_scopes)
    ids = {}
    def identity(scope):
        if scope not in ids:
            ids[scope] = digest(dict(schemaVersion=2, library=library, waveId=wave_id,
                frozenRevision=frozen_revision, scope=scope,
                prerequisiteIds=sorted(identity(parent) for parent in graph[scope])))
        return ids[scope]
    for scope in graph:
        identity(scope)
    return ids


class ObligationLedger:
    def __init__(self, table, wave_id, *, library=None, frozen_revision=None):
        if not isinstance(wave_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', wave_id):
            raise ValueError('Invalid wave identity')
        if bool(library) != bool(frozen_revision):
            raise ValueError('Library and frozen revision must be supplied together')
        self.table, self.wave_id = table, wave_id
        self.library, self.frozen_revision = library or '', frozen_revision or ''
        self.partition = PARTITION_PREFIX + wave_id

    def _get(self, key):
        try:
            return self.table.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None

    def _manifest(self):
        row = self._get(MANIFEST_KEY)
        if row is not None and (row.get('libraryId') != self.library or row.get('frozenRevision') != self.frozen_revision):
            raise RuntimeError('Publication manifest identity mismatch')
        return row

    def record_required(self, obligations, *, prerequisite_scopes=None):
        graph = _graph(obligations.keys(), prerequisite_scopes)
        if (len(set(obligations.values())) != len(obligations)
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
                       for value in obligations.values())):
            raise ValueError('Obligation IDs must be distinct content digests')
        if self.library:
            expected = compute_obligations(self.library, self.wave_id, self.frozen_revision,
                                          obligations.keys(), prerequisite_scopes=graph)
            if expected != obligations:
                raise ValueError('Obligation IDs do not cover the declared wave/revision/dependencies')
        specs = {scope: dict(id=key, prerequisites=sorted(obligations[parent] for parent in graph[scope]))
                 for scope, key in obligations.items()}
        payload = json.dumps(specs, sort_keys=True, separators=(',', ':'))
        if len(payload.encode('utf-16-le')) > 60000:
            raise ValueError('Required obligation manifest exceeds the Table string-property limit')
        identity = digest([self.library, self.wave_id, self.frozen_revision, specs])
        manifest = dict(PartitionKey=self.partition, RowKey=MANIFEST_KEY, schemaVersion=2,
            waveId=self.wave_id, libraryId=self.library, frozenRevision=self.frozen_revision,
            requiredSetId=identity, obligations=payload)
        try:
            self.table.create_entity(manifest)
        except ResourceExistsError:
            current = self._manifest()
            if current['requiredSetId'] != identity or current['obligations'] != payload:
                raise RuntimeError('Required obligation set already captured and differs')
        # Crash anywhere below leaves a full durable manifest. Retry reconstructs
        # missing rows without adopting a smaller set or losing existing receipts.
        for scope, spec in specs.items():
            row = dict(PartitionKey=self.partition, RowKey=spec['id'], waveId=self.wave_id,
                scope=scope, obligationId=spec['id'], requiredSetId=identity, status='pending', receipt='')
            try:
                self.table.create_entity(row)
            except ResourceExistsError:
                current = self._get(spec['id'])
                if current.get('requiredSetId') != identity or current.get('scope') != scope:
                    raise RuntimeError('Existing obligation belongs to another capture')
        return identity

    def _specs(self):
        manifest = self._manifest()
        return (manifest, json.loads(manifest['obligations'])) if manifest else (None, {})

    def record_receipt(self, obligation_id, *, output_generation=None, no_change=False, witness=None):
        if type(no_change) is not bool:
            raise ValueError('no_change must be boolean')
        if no_change:
            if output_generation is not None or not isinstance(witness, str) or not witness.strip():
                raise ValueError('No-change receipt requires a witness and no output generation')
        elif not isinstance(output_generation, str) or not output_generation.strip() or witness is not None:
            raise ValueError('Applied receipt requires an output generation')
        manifest, specs = self._specs()
        spec = next((item for item in specs.values() if item['id'] == obligation_id), None)
        if manifest is None or spec is None:
            raise RuntimeError('Receipt for an obligation that was never required')
        for parent in spec['prerequisites']:
            row = self._get(parent)
            if not self._valid_receipt(row, manifest):
                raise RuntimeError('Publication prerequisite has no committed receipt')
        receipt = json.dumps(dict(outputGeneration=output_generation, noChange=no_change,
                                   witness=witness), sort_keys=True, separators=(',', ':'))
        for _ in range(12):
            row = self._get(obligation_id)
            if row is None or row.get('requiredSetId') != manifest['requiredSetId']:
                raise RuntimeError('Required obligation capture is incomplete; retry record_required')
            if row.get('status') == 'satisfied':
                if row.get('receipt') != receipt:
                    raise RuntimeError('Conflicting receipt for an already satisfied obligation')
                return
            version = etag(row)
            if not version:
                raise RuntimeError('Obligation receipt requires an ETag')
            try:
                self.table.update_entity(dict(row, status='satisfied', receipt=receipt),
                    etag=version, mode=UpdateMode.REPLACE, match_condition=MatchConditions.IfNotModified)
                return
            except ResourceModifiedError:
                continue
        raise RuntimeError('Obligation receipt contention')

    @staticmethod
    def _valid_receipt(row, manifest):
        if (not row or row.get('requiredSetId') != manifest['requiredSetId']
                or row.get('status') != 'satisfied'):
            return False
        try:
            value = json.loads(row.get('receipt') or '')
        except (TypeError, ValueError):
            return False
        if not isinstance(value, dict):
            return False
        return ((value.get('noChange') is True and isinstance(value.get('witness'), str)
                 and bool(value['witness'].strip())
                 and value.get('outputGeneration') is None)
                or (value.get('noChange') is False and isinstance(value.get('outputGeneration'), str)
                    and bool(value['outputGeneration'].strip())
                    and value.get('witness') is None))

    def required_ids(self):
        _, specs = self._specs()
        return {item['id'] for item in specs.values()}

    def is_complete(self):
        manifest, specs = self._specs()
        return manifest is not None and all(self._valid_receipt(self._get(item['id']), manifest)
                                            for item in specs.values())

    def completion_proof(self):
        """Immutable coverage proof; artifact publishers still have to supply receipts."""
        manifest, specs = self._specs()
        if not self.library or manifest is None:
            raise RuntimeError('Completion requires a library/revision-bound manifest')
        receipts = {}
        for spec in specs.values():
            row = self._get(spec['id'])
            if not self._valid_receipt(row, manifest):
                raise RuntimeError('Publication obligations are incomplete')
            receipts[spec['id']] = row['receipt']
        return dict(libraryId=self.library, waveId=self.wave_id, frozenRevision=self.frozen_revision,
                    requiredSetId=manifest['requiredSetId'], receiptDigest=digest(receipts))
