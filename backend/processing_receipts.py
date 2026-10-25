"""Immutable, bounded artifact snapshots, published before source metadata CAS.

A manifest is created only after all snapshot rows are durable. A reference in
committed source metadata/outbox is the publication point; abandoned manifests
are unreferenced. These receipts qualify artifacts, not wave scheduling/policy.
"""
import json
import math
from datetime import datetime

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableTransactionError

from processing_artifacts import verify_receipt
from processing_completion import STEPS, TERMINAL
from processing_fence import current_fence
from upload_waves import digest


class ArtifactReceipts:
    def __init__(self, table, library):
        if table is None:
            raise ValueError('Artifact receipt storage unavailable')
        self.table, self.library = table, library
        self.partition = 'processing-artifacts-' + digest(library)

    def _get(self, key):
        try:
            return self.table.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None

    def _put(self, kind, payload):
        def timestamp(value):
            if isinstance(value, datetime):
                return value.isoformat()
            raise TypeError('Unsupported artifact value')
        encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False, default=timestamp)
        payload = json.loads(encoded)
        if len(encoded.encode('utf-16-le')) > 60_000:
            raise ValueError('Artifact snapshot exceeds property byte budget')
        identity = digest(payload)
        key = kind + '!' + identity
        previous = self._get(key)
        if previous is None:
            row = dict(PartitionKey=self.partition, RowKey=key, payload=encoded)
            try:
                self.table.submit_transaction([('create', row)])
            except (ResourceExistsError, TableTransactionError) as exc:
                if isinstance(exc, TableTransactionError) and exc.status_code != 409:
                    raise
                previous = self._get(key)
                if previous is None:
                    raise
        if previous is not None and previous.get('payload') != encoded:
            raise ValueError('Artifact receipt identity rebound')
        return identity

    def capture(self, storage, metadata):
        fence = current_fence(self.library, metadata['RowKey'])
        if fence is None:
            raise ValueError('Artifact capture requires a source attempt fence')
        fence.check()
        statuses = {step: metadata.get(step + '_status') for step in STEPS}
        from image_utils import is_video_file
        from processing_generation import required_steps_for_media
        required = required_steps_for_media(is_video_file(metadata['RowKey']))
        if any(statuses[step] not in TERMINAL for step in required):
            return None
        # Browser-owned video never runs face detection and can omit its
        # count entirely. Persist the contract's zero-face result with the
        # receipt so completion and frozen face coverage use the same value.
        # Required image face work and positive counts still need proof.
        if 'face' not in required and metadata.get('faceCount') is None:
            metadata['faceCount'] = 0
        derivatives = {kind: verify_receipt(storage, metadata, kind) for kind in ('thumbnail', 'preview')
                       if statuses[kind] == 'done' and (kind in required or metadata.get(kind + 'Artifact'))}
        snapshot_ids = []
        lookup_generation = ''
        if 'face' in required and statuses['face'] in ('done', 'no_data'):
            table = storage._CTX.get('face_by_filename_table_client')
            if table is None:
                raise ValueError('Complete face lookup required for artifact receipt')
            lookup = table.get_entity(self.library, fence.filename)
            ids = storage._validated_face_filename_ids(lookup)
            if (ids is None or len(ids) > 128 or metadata.get('faceCount') != len(ids)
                    or lookup.get('sourcePhotoId') != fence.photo
                    or lookup.get('processingRevision') != fence.revision):
                raise ValueError('Face lookup is not qualified for this source generation')
            lookup_generation = lookup['generation']
            for face_id in sorted(ids):
                face = storage._CTX['face_table_client'].get_entity(self.library, face_id)
                if (face.get('filename') != fence.filename or face.get('sourcePhotoId') != fence.photo
                        or face.get('processingRevision') != fence.revision or not face.get('processingFence')):
                    raise ValueError('Face snapshot source identity mismatch')
                embedding_table = storage._CTX.get('face_embeddings_table_client')
                embedded = embedding_table.get_entity(self.library, face_id) if embedding_table else face
                if any(embedded.get(key) != face.get(key) for key in ('sourcePhotoId', 'processingRevision', 'processingFence')):
                    raise ValueError('Face and embedding attempts differ')
                vector = json.loads(embedded.get('embedding') or '[]')
                if (not isinstance(vector, list) or not vector or not face.get('embeddingVersion')
                        or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)
                        or not any(vector)):
                    raise ValueError('Face snapshot requires a valid versioned embedding')
                snapshot_ids.append(self._put('face', dict(face, embedding=json.dumps(vector, separators=(',', ':')))))
            # Do not publish a receipt from a lookup concurrently replaced by repair.
            again = table.get_entity(self.library, fence.filename)
            if dict(again) != dict(lookup):
                raise ValueError('Face lookup changed during artifact capture')
        elif metadata.get('faceCount') != 0:
            raise ValueError('Skipped face work with existing faces requires an explicit prior receipt')
        fence.check()
        payload = dict(schemaVersion=1, library=self.library, filename=fence.filename, photoId=fence.photo,
            revision=fence.revision, statuses=statuses, derivatives=derivatives, faceCount=metadata.get('faceCount'),
            faceSnapshots=snapshot_ids, faceLookupGeneration=lookup_generation)
        identity = self._put('manifest', payload)
        fence.check()
        return dict(id=identity, library=self.library, filename=fence.filename, photoId=fence.photo, revision=fence.revision)

    def verify_reference(self, reference, intent):
        if (not isinstance(reference, dict) or any(reference.get(key) != intent.get(key)
                for key in ('library', 'filename', 'photoId', 'revision'))):
            raise ValueError('Artifact receipt reference scope mismatch')
        identity = reference.get('id')
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('Invalid artifact manifest identity')
        row = self._get('manifest!' + identity)
        if row is None:
            raise ValueError('Artifact manifest missing')
        payload = json.loads(row['payload'])
        if (digest(payload) != identity or any(payload.get(key) != intent.get(key)
                for key in ('library', 'filename', 'photoId', 'revision', 'statuses', 'derivatives', 'faceCount'))):
            raise ValueError('Artifact manifest does not cover this completion')
        for snapshot in payload['faceSnapshots']:
            face = self._get('face!' + snapshot)
            if face is None or digest(json.loads(face['payload'])) != snapshot:
                raise ValueError('Artifact manifest has missing or invalid face snapshots')
        return payload
