"""Lookup diagnostics must not relax authoritative completeness checks."""
import json

import pytest
from azure.core.exceptions import ServiceRequestError
import storage_utils as storage


@pytest.mark.parametrize('entity,reason', [
    (None, 'missing_row'),
    ({'schemaVersion': 0}, 'schema_mismatch'),
    ({'schemaVersion': 1, 'state': 'writing'}, 'state_writing'),
    ({'schemaVersion': 1, 'state': 'dirty'}, 'state_dirty'),
    ({'schemaVersion': 1, 'state': 'complete', 'generation': 'bad'}, 'invalid_generation'),
    ({'schemaVersion': 1, 'state': 'complete', 'generation': 'a' * 32,
      'leaseExpiresAt': 'not-cleared'}, 'lease_not_cleared'),
    ({'schemaVersion': 1, 'state': 'complete', 'generation': 'a' * 32,
      'leaseExpiresAt': '', 'faceIds': '["a","a"]'}, 'invalid_face_ids'),
])
def test_rejected_lookup_logs_reason_without_accepting_partial_ids(monkeypatch, caplog, entity, reason):
    monkeypatch.setitem(storage._CTX, 'face_by_filename_table_client', object())
    monkeypatch.setattr(storage, '_face_filename_row', lambda *a: entity)
    with caplog.at_level('INFO', logger='storage_utils'):
        assert storage.get_face_ids_for_filename('u', 'a.jpg') is None
    assert 'reason=' + reason in caplog.text


def test_lookup_client_unavailable_reason(monkeypatch, caplog):
    monkeypatch.setitem(storage._CTX, 'face_by_filename_table_client', None)
    with caplog.at_level('INFO', logger='storage_utils'):
        assert storage.get_face_ids_for_filename('u', 'a.jpg') is None
    assert 'reason=client_unavailable' in caplog.text


@pytest.mark.parametrize('ids', [[], ['a']])
def test_complete_lookup_stays_authoritative(monkeypatch, caplog, ids):
    monkeypatch.setitem(storage._CTX, 'face_by_filename_table_client', object())
    row = dict(schemaVersion=1, state='complete', generation='a' * 32,
               leaseExpiresAt='', faceIds=json.dumps(ids))
    monkeypatch.setattr(storage, '_face_filename_row', lambda *a: row)
    with caplog.at_level('INFO', logger='storage_utils'):
        assert storage.get_face_ids_for_filename('u', 'a.jpg') == ids
    assert 'fallback' not in caplog.text


def test_lookup_transport_errors_still_propagate(monkeypatch):
    monkeypatch.setitem(storage._CTX, 'face_by_filename_table_client', object())

    def unavailable(*args):
        raise ServiceRequestError('offline')

    monkeypatch.setattr(storage, '_face_filename_row', unavailable)
    with pytest.raises(ServiceRequestError):
        storage.get_face_ids_for_filename('u', 'a.jpg')