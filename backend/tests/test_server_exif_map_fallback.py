"""Regression test for the server-side exiftool fallback's map_detection step.

_apply_server_exif_fallback runs when the browser's own client-side GPS/EXIF
parser reports the exif step as unsupported/failed/timeout (this is common
for CR3/RAW files, whose ISO-BMFF-style container the bespoke browser parser
handles poorly). It re-extracts EXIF server-side via exiftool and, when GPS
coordinates are found, is supposed to reverse-geocode them into city/country
-- mirroring the sibling branch in _apply_client_processing_results. A prior
bug had this branch mark map_detection 'done' without ever calling the
geocoder, so lat/lon reached the UI but city/country/address never did, and
-- because map_detection was already marked 'done' -- no later retry (browser
or ipworker) could ever fill it in.
"""
from __future__ import annotations

import json

import storage_utils


class _FakeMetadataTable:
    def __init__(self) -> None:
        self.rows: dict = {}

    def upsert_entity(self, entity):
        self.rows[(entity['PartitionKey'], entity['RowKey'])] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        if key not in self.rows:
            raise KeyError(key)
        return dict(self.rows[key])


def _setup_ctx(table):
    storage_utils._CTX.clear()
    storage_utils._CTX['metadata_table_client'] = table
    storage_utils._CTX['blob_service_client'] = object()


def test_server_exif_fallback_geocodes_gps_before_marking_map_detection_done(monkeypatch):
    table = _FakeMetadataTable()
    _setup_ctx(table)
    user_id, filename = 'user-1', '_MG_2875.CR3'
    table.rows[(user_id, filename)] = {
        'PartitionKey': user_id,
        'RowKey': filename,
        'processing_metadata': '{}',
    }

    monkeypatch.setattr(
        storage_utils,
        'extract_exif_from_bytes',
        lambda image_bytes, fname: {
            'GPS.LatitudeDecimal': '55.6027483',
            'GPS.LongitudeDecimal': '12.984635',
        },
    )
    monkeypatch.setattr(
        storage_utils.maps_utils,
        'reverse_geocode',
        lambda lat, lon: {'address': 'Copenhagen, Denmark', 'city': 'Copenhagen', 'region': 'Capital Region', 'country': 'Denmark'},
    )

    metadata: dict = {}
    status_updates = storage_utils._apply_server_exif_fallback(
        user_id, filename, metadata, b'fake-bytes', fallback_for='exif',
    )

    assert status_updates['map_detection_status'] == 'done'
    assert metadata['locationCity'] == 'Copenhagen'
    assert metadata['locationRegion'] == 'Capital Region'
    assert metadata['locationCountry'] == 'Denmark'
    assert metadata['address'] == 'Copenhagen, Denmark'

    stored = table.rows[(user_id, filename)]
    assert stored['map_detection_status'] == 'done'
    processing = json.loads(stored['processing_metadata'])
    assert processing['map_detection']['latitude'] == '55.6027483'
