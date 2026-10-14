import hashlib
import json
from types import SimpleNamespace

import pytest

import upload_metadata as transport


def envelope(ranges=None, size=100):
    ranges = ranges if ranges is not None else [{'offset': 0, 'length': 4}, {'offset': 90, 'length': 2}]
    header = json.dumps({'version': 1, 'kind': 'tiff-ifd-ranges', 'sourceSha256': 'a' * 64,
                         'sourceSize': size, 'ranges': ranges}).encode()
    packet = len(header).to_bytes(4, 'big') + header + b'II42xy'
    descriptor = {'version': 1, 'bytes': len(packet), 'sha256': hashlib.sha256(packet).hexdigest(), 'sourceSha256': 'a' * 64}
    return packet, descriptor


def test_exact_ranges_and_gaps_are_preserved():
    packet, descriptor = envelope()
    assert transport.decode_packet(packet, descriptor, 100) == [(0, b'II42'), (90, b'xy')]


@pytest.mark.parametrize('ranges', [[], [{'offset': 1, 'length': 6}], [{'offset': -1, 'length': 6}],
    [{'offset': 0, 'length': 5}, {'offset': 3, 'length': 1}], [{'offset': 0, 'length': 101}],
    [{'offset': 0, 'length': True}], [{'offset': 0.5, 'length': 6}], [{'offset': 0, 'length': 5}],
    [{'offset': 0, 'length': 6}, {'offset': 99, 'length': 2}]])
def test_rejects_malformed_ranges(ranges):
    packet, descriptor = envelope(ranges)
    with pytest.raises(ValueError):
        transport.decode_packet(packet, descriptor, 100)


def test_rejects_checksum_and_source_mismatch():
    packet, descriptor = envelope()
    with pytest.raises(ValueError):
        transport.decode_packet(packet + b'bad', descriptor, 100)
    with pytest.raises(ValueError):
        transport.decode_packet(packet, descriptor, 99)
    with pytest.raises(ValueError):
        transport.validate_descriptor(descriptor, 'b' * 64)


@pytest.mark.parametrize('changes', [{'version': True}, {'bytes': True}, {'bytes': transport.MAX_PACKET_BYTES + 1}, {'sha256': 'bad'}])
def test_rejects_invalid_descriptor(changes):
    _, descriptor = envelope()
    descriptor.update(changes)
    with pytest.raises(ValueError):
        transport.validate_descriptor(descriptor, 'a' * 64)


def test_approved_large_metadata_descriptor_remains_bounded():
    _, descriptor = envelope()
    descriptor['bytes'] = 4 * 1024 * 1024 + 4096
    assert transport.validate_descriptor(descriptor, 'a' * 64)['bytes'] == descriptor['bytes']
    descriptor['bytes'] = transport.MAX_PACKET_BYTES + 1
    with pytest.raises(ValueError):
        transport.validate_descriptor(descriptor, 'a' * 64)


def test_sparse_reconstruction_and_etag_binding(monkeypatch):
    import exif_utils
    packet, descriptor = envelope()
    candidate = {**descriptor, 'physical': 'physical', 'sourceSize': 100, 'etag': 'etag-1'}
    row = {'uploadMetadataPacket': json.dumps(candidate), 'fileHash': 'a' * 64, 'anonymousImageId': 'physical', 'size': 100}
    calls = []
    def download(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(readall=lambda: packet)
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'thumbnails'},
        _get_blob_client=lambda container, name: SimpleNamespace(download_blob=download))
    def extract(path):
        from pathlib import Path
        rebuilt = Path(path).read_bytes()
        assert len(rebuilt) == 100
        assert rebuilt[:4] == b'II42' and rebuilt[4:90] == b'\0' * 86 and rebuilt[90:92] == b'xy'
        return {'Make': 'Test', 'GPS.LatitudeDecimal': '0', 'GPS.LongitudeDecimal': '0'}
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', extract)
    sizes = []
    outcomes = []
    assert transport.extract_candidate(storage, 'photo.dng', row, sizes.append, outcomes.append)['Make'] == 'Test'
    assert outcomes == ['reused']
    assert sizes == [len(packet)]
    assert calls[0]['etag'] == 'etag-1' and calls[0]['length'] == len(packet)
    for result, reason in [({}, 'no_exif'), ({'FileType': 'DNG'}, 'unqualified_exif'),
                           ({'Make': 'Test', 'Warning': 'Partial'}, 'exiftool_warning'),
                           ({'Make': 'Test', 'EXIF.Truncated': 'true'}, 'exiftool_warning')]:
        monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: result)
        assert transport.extract_candidate(storage, 'photo.dng', row, on_outcome=outcomes.append) is None
        assert outcomes[-1] == reason
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', extract)
    assert transport.extract_candidate(storage, 'photo.dng', row,
        on_outcome=lambda _: (_ for _ in ()).throw(RuntimeError('diagnostics failed')))['Make'] == 'Test'
    assert transport.extract_candidate(storage, 'photo.dng', row,
        on_diagnostic=lambda _: (_ for _ in ()).throw(RuntimeError('diagnostics failed')))['Make'] == 'Test'
    row['anonymousImageId'] = 'replacement'
    assert transport.extract_candidate(storage, 'photo.dng', row, on_outcome=outcomes.append) is None
    assert outcomes[-1] == 'binding_validation_failed'
    assert transport.extract_candidate(storage, 'photo.ari', row, on_outcome=outcomes.append) is None
    assert outcomes[-1] == 'unqualified_family'


def test_candidate_is_durable_without_marking_exif_done(monkeypatch):
    import storage_utils
    monkeypatch.setenv('BROWSER_UPLOAD_PREPARATION', 'true')
    _, descriptor = envelope()
    writes = []
    row = {'anonymousImageId': 'physical', 'fileHash': 'a' * 64, 'size': 100, 'exif_status': 'pending'}
    app = SimpleNamespace(metadata_table_client=SimpleNamespace(get_entity=lambda **_: row),
        _update_metadata_entity_fields=lambda *args: writes.append(args))
    monkeypatch.setitem(storage_utils._CTX, 'blob_thumbnail_container', 'thumbnails')
    monkeypatch.setattr(storage_utils, '_get_blob_client', lambda *args: SimpleNamespace(get_blob_properties=lambda:
        SimpleNamespace(size=descriptor['bytes'], content_settings=SimpleNamespace(content_type='application/octet-stream'), etag='etag-1')))
    transport.stamp_candidate(app, 'library', 'photo.dng', descriptor, 'physical', 100, 'a' * 64)
    assert writes[0][:2] == ('library', 'photo.dng')
    assert json.loads(writes[0][2]['uploadMetadataPacket'])['physical'] == 'physical'
    assert 'exif_status' not in writes[0][2]
    with pytest.raises(ValueError):
        transport.stamp_candidate(app, 'library', 'photo.dng', descriptor, 'other', 100, 'a' * 64)


@pytest.mark.parametrize('extension', ['crw', 'fff', 'iiq', 'mef', 'nrw', 'orf', 'ori', 'raw', 'srw', 'tif', 'tiff', 'sti', 'x3f', 'heic', 'heif', 'hif'])
def test_corpus_verified_families_are_offered_packets(extension):
    assert transport.qualified_family('photo.' + extension)
    assert transport.qualified_family('photo.' + extension.upper())


@pytest.mark.parametrize('exif,category', [
    ({'Warning': 'Invalid original decision data'}, 'canon_decision_data'),
    ({'Warning': '[minor] Invalid VignettingCorrUnknown1 data'}, 'canon_vignetting'),
    ({'Warning': '[Minor] Not decoding some large array(s). Ignore minor errors to decode'}, 'large_array_limit'),
    ({'Warning': 'private EXIF content that must not be logged'}, 'unclassified_warning'),
    ({'Error': 'private error content'}, 'parser_error'),
    ({'EXIF.Truncated': 'true'}, 'storage_limit'),
    ({}, 'none'),
])
def test_extraction_diagnostics_are_bounded_and_do_not_expose_values(exif, category):
    info = transport._extraction_diagnostic({**exif, 'ExifToolVersion': '13.55', 'GPS.LatitudeDecimal': 'private'})
    assert info == {'version': '13.55', 'category': category}
    assert transport._extraction_diagnostic({'ExifToolVersion': 'private'})['version'] == 'unknown'
