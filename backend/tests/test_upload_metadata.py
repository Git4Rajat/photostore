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


@pytest.mark.parametrize('extension', ['crw', 'fff', 'iiq', 'mef', 'nrw', 'orf', 'ori', 'raw', 'srw', 'tif', 'tiff', 'sti', 'x3f', 'heic', 'heif', 'hif', 'png'])
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


def _candidate_fixture():
    packet, descriptor = envelope()
    candidate = {**descriptor, 'physical': 'physical', 'sourceSize': 100, 'etag': 'etag-1'}
    row = {'uploadMetadataPacket': json.dumps(candidate), 'fileHash': 'a' * 64, 'anonymousImageId': 'physical', 'size': 100}
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'thumbnails'},
        _get_blob_client=lambda *_: SimpleNamespace(download_blob=lambda **_: SimpleNamespace(readall=lambda: packet)))
    return storage, row


def _proraw():
    return {'Make': 'Apple', 'Model': 'iPhone 12 Pro Max', 'FileType': 'DNG', 'DNGVersion': '1 6 0 0',
            'ExifToolVersion': '13.55',
            'Warning': '[Minor] Not decoding some large array(s). Ignore minor errors to decode'}


@pytest.mark.parametrize('model,version', [('iPhone 12 Pro', '1 4 0 0'), ('iPhone 12 Pro Max', '1 6 0 0')])
def test_verified_proraw_warning_reuses_packet_without_dropping_warning(monkeypatch, model, version):
    import exif_utils
    storage, row = _candidate_fixture()
    exif = {**_proraw(), 'Model': model, 'DNGVersion': version}
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: exif.copy())
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warning', lambda path, warning: warning == exif['Warning'])
    outcomes, diagnostics = [], []
    assert transport.extract_candidate(storage, 'photo.DNG', row, on_outcome=outcomes.append,
        on_diagnostic=diagnostics.append) == exif
    assert outcomes == ['reused']
    assert diagnostics == [{'version': '13.55', 'category': 'large_array_limit'}]


def test_proraw_hidden_additional_warning_or_diagnostic_failure_rejects(monkeypatch):
    import exif_utils
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: _proraw())
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warning', lambda *_: False)
    assert transport.extract_candidate(storage, 'photo.dng', row) is None


@pytest.mark.parametrize('changes', [
    {'Warning': 'JPEG format error'}, {'Warning': _proraw()['Warning'] + '; corrupted'},
    {'Warning': ''}, {'ExifToolVersion': '13.56'}, {'ExifToolVersion': 'unknown'},
    {'Make': 'Samsung'}, {'Model': 'iPhone 15 Pro'}, {'FileType': 'TIFF'},
    {'DNGVersion': '1 7 0 0'}, {'Error': 'Invalid data'}, {'EXIF.Truncated': 'true'},
])
def test_proraw_exception_never_accepts_other_warnings_profiles_or_truncation(monkeypatch, changes):
    import exif_utils
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: {**_proraw(), **changes})
    outcomes = []
    assert transport.extract_candidate(storage, 'photo.dng', row, on_outcome=outcomes.append) is None
    assert outcomes == ['exiftool_warning']
    assert not transport._verified_proraw_array_warning('photo.jpg', _proraw())


def _png_exif():
    return {'FileType': 'PNG', 'ImageWidth': '900', 'ImageHeight': '705', 'BitDepth': '8',
            'ColorType': '2', 'Compression': '0', 'Filter': '0', 'Interlace': '0', 'ProfileName': 'Display P3'}


def test_png_without_camera_tags_reuses_parsed_metadata(monkeypatch):
    import exif_utils
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: _png_exif())
    assert transport.extract_candidate(storage, 'screen.PNG', row) == _png_exif()
    assert not transport._qualified_png_metadata('screen.jpg', _png_exif())


@pytest.mark.parametrize('changes', [
    {'FileType': 'JPEG'}, {'ImageWidth': '0'}, {'ImageHeight': '-1'}, {'ImageHeight': 'NaN'}, {'ImageWidth': '2147483648'},
    {'BitDepth': '3'}, {'ColorType': '1'}, {'Compression': '1'}, {'Filter': '1'}, {'Interlace': '2'},
    {'Warning': 'Partial'}, {'Error': 'Bad CRC'}, {'EXIF.Truncated': 'true'},
])
def test_incomplete_png_metadata_still_falls_back(monkeypatch, changes):
    import exif_utils
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: {**_png_exif(), **changes})
    assert transport.extract_candidate(storage, 'screen.png', row) is None


def test_png_file_tags_alone_never_establish_exif_absence(monkeypatch):
    import exif_utils
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: {'FileType': 'PNG', 'FileSize': '100'})
    assert transport.extract_candidate(storage, 'screen.png', row) is None
