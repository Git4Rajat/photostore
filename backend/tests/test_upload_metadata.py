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
    assert not transport._verified_dng_array_warning('photo.jpg', _proraw())


def _profile_dng(camera):
    warning = '[Minor] Not decoding some large array(s). Ignore minor errors to decode'
    if camera == 'canon':
        return {'Make': 'Canon', 'Model': 'Canon EOS 5D Mark III', 'FileType': 'DNG',
                'DNGVersion': '1 4 0 0', 'ExifToolVersion': '13.55', 'Warning': warning + ' [x3]',
                'ProfileHueSatMapData1': '(large array of 8100 float values)',
                'ProfileHueSatMapData2': '(large array of 8100 float values)',
                'ProfileLookTableData': '(large array of 13824 float values)'}
    return {'Make': 'Autel Robotics', 'Model': 'XB015', 'FileType': 'DNG',
            'DNGVersion': '1 4 0 0', 'ExifToolVersion': '13.55', 'Warning': warning + ' [x2]',
            'ProfileHueSatMapData1': '(large array of 2250 float values)',
            'ProfileHueSatMapData2': '(large array of 2250 float values)'}


@pytest.mark.parametrize('camera', ['canon', 'autel'])
def test_verified_profile_arrays_reuse_packet_without_expanding_or_dropping_warning(monkeypatch, camera):
    import exif_utils
    storage, row = _candidate_fixture()
    exif = {**_profile_dng(camera), 'DateTimeOriginal': '2020:01:02 03:04:05',
            'GPS.LatitudeDecimal': '12.34', 'GPS.LongitudeDecimal': '56.78'}
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: exif.copy())
    checks = []
    def check(path, warning):
        checks.append((path, warning))
        return warning == exif['Warning']
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warning', check)
    outcomes, diagnostics, sizes = [], [], []
    assert transport.extract_candidate(storage, 'photo.DNG', row, sizes.append,
        outcomes.append, diagnostics.append) == exif
    assert len(checks) == 1 and checks[0][1] == exif['Warning']
    assert sizes == [len(envelope()[0])]
    assert outcomes == ['reused']
    assert diagnostics == [{'version': '13.55', 'category': 'large_array_limit'}]


@pytest.mark.parametrize('camera', ['canon', 'autel'])
@pytest.mark.parametrize('changes', [
    {'Warning': 'Invalid EXIF text encoding for UserComment'},
    {'Warning': '[Minor] Not decoding some large array(s). Ignore minor errors to decode'},
    {'Warning': '[Minor] Not decoding some large array(s). Ignore minor errors to decode [x4]'},
    {'ExifToolVersion': '13.56'}, {'ExifToolVersion': 'unknown'},
    {'Make': 'Unknown'}, {'Model': 'Unknown'}, {'DNGVersion': '1 6 0 0'}, {'FileType': 'TIFF'},
    {'Error': 'Bad offset'}, {'EXIF.Truncated': 'true'},
    {'ProfileHueSatMapData1': '(large array of 99999 float values)'},
    {'ProfileHueSatMapData2': '(large array of 2250 double values)'},
    {'ProfileHueSatMapData1': None}, {'ProfileHueSatMapData2': None},
])
def test_profile_array_exception_rejects_unverified_data(monkeypatch, camera, changes):
    import exif_utils
    storage, row = _candidate_fixture()
    exif = {**_profile_dng(camera), **changes}
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: exif.copy())
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warning', lambda *_: pytest.fail('Unqualified profile must not be exempted'))
    outcomes = []
    assert transport.extract_candidate(storage, 'photo.dng', row, on_outcome=outcomes.append) is None
    assert outcomes == ['exiftool_warning']


@pytest.mark.parametrize('value', [None, '(large array of 13824 double values)', '(large array of 13825 float values)'])
def test_canon_profile_requires_verified_look_table(monkeypatch, value):
    import exif_utils
    storage, row = _candidate_fixture()
    exif = {**_profile_dng('canon'), 'ProfileLookTableData': value}
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: exif.copy())
    assert transport.extract_candidate(storage, 'photo.dng', row) is None


@pytest.mark.parametrize('camera', ['canon', 'autel'])
@pytest.mark.parametrize('failure', ['additional_warning', 'check_exception'])
def test_profile_arrays_still_require_independent_diagnostic_check(monkeypatch, camera, failure):
    import exif_utils
    storage, row = _candidate_fixture()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: _profile_dng(camera))
    def check(*_):
        if failure == 'check_exception':
            raise RuntimeError('Diagnostic check failed')
        return False
    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warning', check)
    assert transport.extract_candidate(storage, 'photo.dng', row) is None


@pytest.mark.parametrize('camera', ['canon', 'autel'])
def test_profile_array_exception_is_dng_only(camera):
    assert not transport._verified_dng_array_warning('photo.tif', _profile_dng(camera))


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


def _jpeg_candidate(ranges=None, progressive=False, source=None):
    import io
    from PIL import Image
    if source is None:
        stream = io.BytesIO()
        Image.new('RGB', (40, 20), 'red').save(stream, format='JPEG', progressive=progressive)
        source = stream.getvalue()
    if ranges is None:
        ranges = []
        pos, start = 2, 0
        while source[pos:pos + 2] != b'\xff\xd9':
            marker = source[pos + 1]
            pos += 2 + int.from_bytes(source[pos + 2:pos + 4], 'big')
            if marker == 0xda:
                ranges.append((start, source[start:pos]))
                while True:
                    pos = source.index(b'\xff', pos)
                    following = source[pos + 1]
                    if following == 0 or 0xd0 <= following <= 0xd7:
                        pos += 2
                    elif following == 255:
                        pos += 1
                    else:
                        break
                start = pos
        ranges.append((start, source[start:]))
    header = json.dumps({'version': 1, 'kind': 'jpeg-header-ranges', 'sourceSha256': 'a' * 64,
        'sourceSize': len(source), 'ranges': [{'offset': offset, 'length': len(data)} for offset, data in ranges]}).encode()
    packet = len(header).to_bytes(4, 'big') + header + b''.join(data for _, data in ranges)
    descriptor = {'version': 1, 'bytes': len(packet), 'sha256': hashlib.sha256(packet).hexdigest(), 'sourceSha256': 'a' * 64}
    row = {'uploadMetadataPacket': json.dumps({**descriptor, 'physical': 'physical', 'sourceSize': len(source), 'etag': 'etag-1'}),
        'fileHash': 'a' * 64, 'anonymousImageId': 'physical', 'size': len(source)}
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'thumbnails'},
        _get_blob_client=lambda *_: SimpleNamespace(download_blob=lambda **_: SimpleNamespace(readall=lambda: packet)))
    return storage, row, ranges, source


def _jpeg_exif():
    return {'FileType': 'JPEG', 'ImageWidth': '40', 'ImageHeight': '20', 'JFIFVersion': '1 1'}


def test_complete_jpeg_without_exif_reuses_structural_metadata(monkeypatch):
    import exif_utils
    storage, row, _, _ = _jpeg_candidate()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: _jpeg_exif())
    outcomes = []
    assert transport.extract_candidate(storage, 'plain.JPEG', row, on_outcome=outcomes.append) == _jpeg_exif()
    assert outcomes == ['reused_no_exif']


@pytest.mark.parametrize('fault', ['header-only', 'hole', 'wrong-end', 'application', 'frame', 'scan', 'jfif', 'trailer'])
def test_jpeg_absence_requires_complete_qualified_structure(monkeypatch, fault):
    import exif_utils
    _, _, ranges, source = _jpeg_candidate()
    header = bytearray(ranges[0][1])
    if fault == 'header-only':
        ranges = ranges[:1]
    elif fault == 'hole':
        ranges = [(0, bytes(header[:20])), (21, bytes(header[21:])), ranges[-1]]
    elif fault == 'wrong-end':
        ranges[-1] = (len(source) - 3, b'\xff\xd9')
    elif fault == 'application':
        header[3] = 0xe1
        ranges[0] = (0, bytes(header))
    elif fault == 'frame':
        index = header.index(b'\xff\xc0')
        header[index + 8] = 39
        ranges[0] = (0, bytes(header))
    elif fault == 'scan':
        index = header.index(b'\xff\xda')
        header[index + 4] = 4
        ranges[0] = (0, bytes(header))
    elif fault == 'jfif':
        header[6:11] = b'JFXX\0'
        ranges[0] = (0, bytes(header))
    else:
        ranges[-1] = (len(source) - 2, b'xx')
    storage, row, _, _ = _jpeg_candidate(ranges)
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: _jpeg_exif())
    assert transport.extract_candidate(storage, 'plain.jpg', row) is None


@pytest.mark.parametrize('changes', [
    {'ImageWidth': '0'}, {'ImageHeight': '21'}, {'FileType': 'TIFF'},
    {'Warning': 'Partial'}, {'Error': 'Bad JPEG'}, {'EXIF.Truncated': 'true'},
])
def test_jpeg_no_exif_still_rejects_parser_errors_and_mismatched_dimensions(monkeypatch, changes):
    import exif_utils
    storage, row, _, _ = _jpeg_candidate()
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: {**_jpeg_exif(), **changes})
    assert transport.extract_candidate(storage, 'plain.jpg', row) is None


def test_jpeg_no_exif_is_not_inferred_for_other_formats():
    _, _, ranges, source = _jpeg_candidate()
    assert not transport._verified_jpeg_no_exif('photo.dng', ranges, len(source), _jpeg_exif())


@pytest.mark.parametrize('progressive', [False, True])
def test_complete_jpeg_packet_with_real_exiftool(tmp_path, progressive):
    import shutil
    import exif_utils
    if not shutil.which('exiftool'):
        pytest.skip('Real ExifTool not installed')
    storage, row, _, source = _jpeg_candidate(progressive=progressive)
    original_path = tmp_path / 'plain.jpg'
    original_path.write_bytes(source)
    excluded = {'FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'}
    expected = {key: value for key, value in exif_utils._extract_exiftool_from_path(str(original_path)).items() if key not in excluded}
    outcomes = []
    assert transport.extract_candidate(storage, 'plain.jpg', row, on_outcome=outcomes.append) == expected
    assert outcomes == ['reused_no_exif']


@pytest.mark.parametrize('marker,data', [
    (0xe1, b'http://ns.adobe.com/xap/1.0/\x00metadata'), (0xe2, b'ICC_PROFILE\x00\x01\x01profile'),
    (0xec, b'Ducky\x00metadata'), (0xed, b'Photoshop 3.0\x00metadata'),
    (0xee, b'Adobe' + bytes(7)), (0xee, b'Adobe' + bytes(31)), (0xfe, b'Comment'),
])
def test_complete_metadata_jpeg_is_reused_but_not_reported_as_no_exif(monkeypatch, marker, data):
    import exif_utils
    _, _, _, source = _jpeg_candidate()
    segment = bytes([255, marker]) + (len(data) + 2).to_bytes(2, 'big') + data
    storage, row, ranges, source = _jpeg_candidate(source=source[:2] + segment + source[2:])
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: _jpeg_exif())
    outcomes = []
    assert transport.extract_candidate(storage, 'photo.jpg', row, on_outcome=outcomes.append) == _jpeg_exif()
    assert outcomes == ['reused']
    assert not transport._verified_jpeg_no_exif('photo.jpg', ranges, len(source), _jpeg_exif())
    assert not transport._verified_jpeg_structure('photo.jpg', ranges[:1], len(source), _jpeg_exif(), metadata=True)


@pytest.mark.parametrize('mode,progressive', [('RGB', False), ('RGB', True), ('CMYK', False)])
def test_metadata_jpeg_exact_parity_with_real_exiftool(tmp_path, mode, progressive):
    import io
    import shutil
    from PIL import Image, ImageCms
    import exif_utils
    if not shutil.which('exiftool'):
        pytest.skip('Real ExifTool not installed')
    exif = Image.Exif(); exif[305] = 'Local parity fixture'
    output = io.BytesIO()
    Image.new(mode, (40, 20)).save(output, 'JPEG', exif=exif, progressive=progressive,
        icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes(), comment=b'Preserved comment')
    source = output.getvalue()
    storage, row, _, _ = _jpeg_candidate(source=source)
    path = tmp_path / 'metadata.jpg'; path.write_bytes(source)
    excluded = {'FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'}
    expected = {k: v for k, v in exif_utils._extract_exiftool_from_path(str(path)).items() if k not in excluded}
    outcomes = []
    assert transport.extract_candidate(storage, 'metadata.jpg', row, on_outcome=outcomes.append) == expected
    assert outcomes == ['reused']


def _webp_candidate(source=None, ranges=None, lossless=False, alpha=False):
    import io
    from PIL import Image, ImageCms
    if source is None:
        output = io.BytesIO()
        exif = Image.Exif(); exif[305] = 'Local parity fixture'
        Image.new('RGBA' if alpha else 'RGB', (40, 20), (10, 20, 30, 128) if alpha else (10, 20, 30)).save(
            output, 'WEBP', lossless=lossless, exif=exif,
            icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes(),
            xmp=b'<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')
        source = output.getvalue()
    if ranges is None:
        ranges, pos = [(0, source[:12])], 12
        while pos < len(source):
            kind = source[pos:pos + 4]
            length = int.from_bytes(source[pos + 4:pos + 8], 'little')
            prefix = min(length, {b'VP8 ': 10, b'VP8L': 5, b'ALPH': 1}.get(kind, length))
            ranges.append((pos, source[pos:pos + 8 + prefix]))
            if length & 1:
                ranges.append((pos + 8 + length, source[pos + 8 + length:pos + 9 + length]))
            pos += 8 + length + (length & 1)
    header = json.dumps({'version': 1, 'kind': 'webp-chunk-ranges', 'sourceSha256': 'a' * 64,
        'sourceSize': len(source), 'ranges': [{'offset': pos, 'length': len(data)} for pos, data in ranges]}).encode()
    packet = len(header).to_bytes(4, 'big') + header + b''.join(data for _, data in ranges)
    descriptor = {'version': 1, 'bytes': len(packet), 'sha256': hashlib.sha256(packet).hexdigest(), 'sourceSha256': 'a' * 64}
    row = {'uploadMetadataPacket': json.dumps({**descriptor, 'physical': 'local', 'sourceSize': len(source), 'etag': 'local'}),
           'fileHash': 'a' * 64, 'anonymousImageId': 'local', 'size': len(source)}
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'local'}, _get_blob_client=lambda *_:
        SimpleNamespace(download_blob=lambda **_: SimpleNamespace(readall=lambda: packet)))
    return storage, row, ranges, source


@pytest.mark.parametrize('lossless', [False, True])
@pytest.mark.parametrize('alpha', [False, True])
def test_webp_exact_parity_with_real_exiftool(tmp_path, lossless, alpha):
    import shutil
    import exif_utils
    if not shutil.which('exiftool'):
        pytest.skip('Real ExifTool not installed')
    storage, row, _, source = _webp_candidate(lossless=lossless, alpha=alpha)
    path = tmp_path / 'photo.webp'; path.write_bytes(source)
    excluded = {'FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'}
    expected = {k: v for k, v in exif_utils._extract_exiftool_from_path(str(path)).items() if k not in excluded}
    outcomes = []
    assert transport.extract_candidate(storage, 'photo.webp', row, on_outcome=outcomes.append) == expected
    assert outcomes == ['reused']


@pytest.mark.parametrize('fault', ['size', 'flags', 'prefix', 'metadata-hole', 'dimensions', 'trailer', 'warning', 'unknown'])
def test_webp_requires_complete_structure_even_with_camera_identity(monkeypatch, fault):
    import exif_utils
    _, _, ranges, source = _webp_candidate()
    ranges = [(pos, bytearray(data)) for pos, data in ranges]
    exif = {'FileType': 'WEBP', 'ImageWidth': '40', 'ImageHeight': '20', 'Make': 'Test'}
    if fault == 'size': ranges[0][1][4] ^= 1
    if fault == 'flags': ranges[1][1][8] |= 2
    if fault == 'prefix': next(data for _, data in ranges if data[:4] == b'VP8 ')[11] = 0
    if fault == 'metadata-hole':
        ranges = [(pos, data[:9] if data[:4] == b'EXIF' else data) for pos, data in ranges]
    if fault == 'dimensions': exif['ImageHeight'] = '21'
    if fault == 'trailer': ranges[-1] = (ranges[-1][0], ranges[-1][1][:-1])
    if fault == 'warning': exif['Warning'] = 'Partial metadata'
    if fault == 'unknown': next(data for _, data in ranges if data[:4] == b'EXIF')[:4] = b'JUNK'
    storage, row, _, _ = _webp_candidate(source=source, ranges=ranges)
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: exif.copy())
    assert transport.extract_candidate(storage, 'photo.webp', row) is None
