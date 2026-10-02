"""Unit tests for _face_ids_awaiting_person_assignment's keyed-lookup path.

Mirrors test_face_by_filename_lookup.py's storage_utils-side coverage, but
for the app.py reader: it must return identical results whether or not the
photofacebyfilename lookup table is configured, and must only ever use the
lookup to narrow fresh ownership reads, never a stale whole-partition
summary or the lookup row itself to decide personId status.
"""
from __future__ import annotations

import json

import pytest

import app
import storage_utils
from azure.core.exceptions import ResourceNotFoundError
from fakes import FakeTable, ResourceNotFound


class LookupTable(FakeTable):
    def get_entity(self, partition_key, row_key):
        try:
            return super().get_entity(partition_key, row_key)
        except ResourceNotFound as exc:
            raise ResourceNotFoundError(str(exc)) from exc


@pytest.fixture(autouse=True)
def face_tables(monkeypatch):
    face_table = FakeTable()
    lookup_table = LookupTable()
    monkeypatch.setattr(app, 'face_table_client', face_table)
    monkeypatch.setattr(app, 'face_by_filename_table_client', lookup_table)
    monkeypatch.setitem(storage_utils._CTX, 'face_by_filename_table_client', lookup_table)
    monkeypatch.setattr(app, '_face_summary_scan_cache', app._UserScanCache(app.PEOPLE_SCAN_CACHE_TTL_SECONDS))
    return face_table, lookup_table


def _seed_face(face_table, user_id, face_id, filename, *, person_id=None):
    row = {
        'PartitionKey': user_id,
        'RowKey': face_id,
        'filename': filename,
    }
    if person_id:
        row['personId'] = person_id
    face_table.upsert_entity(row)


def test_returns_unassigned_faces_via_keyed_lookup(face_tables):
    face_table, lookup_table = face_tables
    _seed_face(face_table, 'u1', 'face-1', 'photo.jpg')
    _seed_face(face_table, 'u1', 'face-2', 'photo.jpg', person_id='person-1')
    _seed_face(face_table, 'u1', 'face-3', 'other.jpg')
    lookup_table.upsert_entity({
        'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
        'faceIds': json.dumps(['face-1', 'face-2']),
        'schemaVersion': 1, 'state': 'complete', 'generation': 'a' * 32, 'leaseExpiresAt': '',
    })

    result = app._face_ids_awaiting_person_assignment('u1', 'photo.jpg')
    assert result == ['face-1']


def test_falls_back_to_fresh_filename_query_when_lookup_row_missing(face_tables):
    """No lookup row yet (e.g. a photo never touched since this table was
    introduced) must still find its faces via a fresh filename query,
    not be treated as having zero faces."""
    face_table, _lookup_table = face_tables
    _seed_face(face_table, 'u1', 'face-1', 'photo.jpg')

    result = app._face_ids_awaiting_person_assignment('u1', 'photo.jpg')
    assert result == ['face-1']


def test_falls_back_when_lookup_table_unconfigured(monkeypatch, face_tables):
    face_table, _lookup_table = face_tables
    monkeypatch.setattr(app, 'face_by_filename_table_client', None)
    monkeypatch.setitem(storage_utils._CTX, 'face_by_filename_table_client', None)
    _seed_face(face_table, 'u1', 'face-1', 'photo.jpg')

    result = app._face_ids_awaiting_person_assignment('u1', 'photo.jpg')
    assert result == ['face-1']


def test_returns_empty_list_when_all_faces_assigned(face_tables):
    face_table, lookup_table = face_tables
    _seed_face(face_table, 'u1', 'face-1', 'photo.jpg', person_id='person-1')
    lookup_table.upsert_entity({
        'PartitionKey': 'u1', 'RowKey': 'photo.jpg',
        'faceIds': json.dumps(['face-1']),
        'schemaVersion': 1, 'state': 'complete', 'generation': 'a' * 32, 'leaseExpiresAt': '',
    })

    assert app._face_ids_awaiting_person_assignment('u1', 'photo.jpg') == []


def test_complete_lookup_does_not_load_library_summary(face_tables, monkeypatch):
    face_table, lookup_table = face_tables
    _seed_face(face_table, 'u1', 'face-1', 'photo.jpg')
    lookup_table.upsert_entity({
        'PartitionKey': 'u1', 'RowKey': 'photo.jpg', 'faceIds': '["face-1"]',
        'schemaVersion': 1, 'state': 'complete', 'generation': 'a' * 32, 'leaseExpiresAt': '',
    })
    def unexpected(*args):
        raise AssertionError('Whole-library summary must not be loaded')
    monkeypatch.setattr(app, '_load_user_face_summary_by_id', unexpected)
    assert app._face_ids_awaiting_person_assignment('u1', 'photo.jpg') == ['face-1']
