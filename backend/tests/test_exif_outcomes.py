"""EXIF failures must stay distinguishable from a readable raster without EXIF."""
import io
import json
import shutil
import struct
import subprocess
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PIL import Image, ImageFile, PngImagePlugin

import exif_utils
import ipwork_geo


def _tool_result(monkeypatch, stdout, *, returncode=0, stderr=b''):
    calls = []
    monkeypatch.setattr(exif_utils.shutil, 'which', lambda _: '/test/exiftool')

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(exif_utils.subprocess, 'run', run)
    return calls


def _raster_bytes(format='JPEG', **kwargs):
    stream = io.BytesIO()
    Image.new('RGB', (40, 20), 'red').save(stream, format, **kwargs)
    return stream.getvalue()


@pytest.mark.parametrize('stdout,returncode,category', [
    (b'private parser output', 1, 'parser_error'),
    (b'', 0, 'invalid_output'),
    (b'not JSON', 0, 'invalid_output'),
    (b'{}', 0, 'invalid_output'),
    (b'[]', 0, 'invalid_output'),
    (b'[null]', 0, 'invalid_output'),
    (b'[{}, {}]', 0, 'invalid_output'),
    (b'[{}]', 0, 'invalid_output'),
    (b'[{"SourceFile": "private/path"}]', 0, 'invalid_output'),
    (b'[{"Error": "private parser output"}]', 0, 'parser_error'),
    (b'[{"ExifTool:Error": "private parser output", "IFD0:Make": "Camera"}]', 0, 'parser_error'),
    (b'[{"Error": ""}]', 0, 'parser_error'),
])
def test_exiftool_failure_is_categorized(monkeypatch, stdout, returncode, category):
    _tool_result(monkeypatch, stdout, returncode=returncode)
    with pytest.raises(exif_utils.ExifExtractionError) as caught:
        exif_utils._extract_exiftool_from_path('private/path.dng')
    assert caught.value.category == category
    assert str(caught.value) == 'EXIF extraction failed: ' + category


@pytest.mark.parametrize('tag,message', [
    ('Error', 'Unknown file type'),
    ('ExifTool:Error', 'Unsupported RAW file type'),
    ('ExifTool:Error', 'Unsupported file type'),
    ('ExifTool:Warning', 'Unsupported file type'),
])
@pytest.mark.parametrize('returncode', [0, 1])
def test_explicit_unsupported_format_is_failure_even_with_zero_exit(monkeypatch, tag, message, returncode):
    raw = {tag: message, 'File:FileType': 'LRI'}
    _tool_result(monkeypatch, json.dumps([raw]).encode(), returncode=returncode)
    with pytest.raises(exif_utils.ExifExtractionError) as caught:
        exif_utils._extract_exiftool_from_path('sample_001.lri')
    assert caught.value.category == 'unsupported'
    assert str(caught.value) == 'EXIF extraction failed: unsupported'
    expected = {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_unsupported'}
    assert ipwork_geo.process_exif('u', 'sample_001.lri', b'unsupported container') == expected
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Unsupported metadata'))
    assert ipwork_geo.process_geo('u', 'sample_001.lri', b'unsupported container') == expected


@pytest.mark.parametrize('data', [
    {'Error': 'Unknown file type'},
    {'Error': 'Unsupported RAW file type'},
    {'Error': 'Unsupported file type'},
    {'FileType': 'LRI', 'Warning': 'Unsupported file type'},
])
def test_processor_defensively_classifies_unsupported_dictionaries(monkeypatch, data):
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: data)
    expected = {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_unsupported'}
    assert ipwork_geo.process_exif('u', 'sample_001.lri', b'data') == expected
    assert ipwork_geo.process_geo('u', 'sample_001.lri', b'data') == expected


@pytest.mark.parametrize('message', ['Unsupported file type [x2]', 'unsupported file type', 'Other benign warning'])
def test_other_warning_text_keeps_existing_normalized_output(monkeypatch, message):
    _tool_result(monkeypatch, json.dumps([{'ExifTool:Warning': message, 'IFD0:Make': 'Camera'}]).encode())
    assert exif_utils._extract_exiftool_from_path('photo.dng') == {'Warning': message, 'Make': 'Camera'}


@pytest.mark.parametrize('failure,category', [
    (subprocess.TimeoutExpired('private command', 1, output=b'private'), 'timeout'),
    (FileNotFoundError('private executable'), 'unavailable'),
    (PermissionError('private executable'), 'execution_failed'),
])
@pytest.mark.parametrize('entrypoint', ['tool', 'path', 'bytes'])
def test_tool_execution_failures_survive_public_entrypoints(monkeypatch, failure, category, entrypoint):
    _tool_result(monkeypatch, b'')

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(exif_utils.subprocess, 'run', fail)
    with pytest.raises(exif_utils.ExifExtractionError) as caught:
        if entrypoint == 'tool':
            exif_utils._extract_exiftool_from_path('missing.dng')
        elif entrypoint == 'path':
            exif_utils.extract_exif_from_path('missing.dng')
        else:
            exif_utils.extract_exif_from_bytes(b'unreadable container', 'photo.dng')
    assert caught.value.category == category
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('entrypoint', ['tool', 'path', 'bytes'])
def test_unavailable_tool_does_not_establish_absence(monkeypatch, entrypoint):
    monkeypatch.setattr(exif_utils.shutil, 'which', lambda _: None)
    with pytest.raises(exif_utils.ExifExtractionError, match='unavailable'):
        if entrypoint == 'tool':
            exif_utils._extract_exiftool_from_path('missing.dng')
        elif entrypoint == 'path':
            exif_utils.extract_exif_from_path('missing.dng')
        else:
            exif_utils.extract_exif_from_bytes(b'unreadable container', 'photo.dng')


@pytest.mark.parametrize('stdout,returncode,category', [
    (b'failure', 1, 'parser_error'),
    (b'[{"ExifTool:Error": "Bad offsets"}]', 0, 'parser_error'),
    (b'broken JSON', 0, 'invalid_output'),
])
@pytest.mark.parametrize('entrypoint', ['path', 'bytes'])
def test_parser_failures_cannot_become_empty_metadata(monkeypatch, stdout, returncode, category, entrypoint):
    _tool_result(monkeypatch, stdout, returncode=returncode)
    with pytest.raises(exif_utils.ExifExtractionError) as caught:
        if entrypoint == 'path':
            exif_utils.extract_exif_from_path('photo.dng')
        else:
            exif_utils.extract_exif_from_bytes(b'unsupported container', 'photo.dng')
    assert caught.value.category == category


@pytest.mark.parametrize('trailer', [False, True])
def test_empty_jpeg_pillow_read_needs_exiftool_before_claiming_absence(monkeypatch, tmp_path, trailer):
    monkeypatch.setattr(exif_utils.shutil, 'which', lambda _: None)
    data = _jpeg_with_late_exif() if trailer else _raster_bytes()
    path = tmp_path / 'plain.jpg'
    path.write_bytes(data)
    with pytest.raises(exif_utils.ExifExtractionError, match='unavailable'):
        exif_utils.extract_exif_from_bytes(data, path.name)
    with pytest.raises(exif_utils.ExifExtractionError, match='unavailable'):
        exif_utils.extract_exif_from_path(str(path))
    expected = {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_unavailable'}
    assert ipwork_geo.process_exif('u', path.name, data) == expected
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Unverified coordinates'))
    assert ipwork_geo.process_geo('u', path.name, data) == expected


def test_truly_verified_empty_extraction_sets_trusted_provenance(monkeypatch):
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: {})
    assert ipwork_geo.process_exif('u', 'plain.jpg', b'original') == {
        'hasData': False, 'source': 'server_metadata', 'verifiedNoExif': True}


def test_empty_pillow_read_uses_full_tool_without_decoding_pixels_or_holding_lock(monkeypatch):
    data = _raster_bytes()
    calls = []

    def fail(*args, **kwargs):
        pytest.fail('EXIF extraction must not decode pixels')

    lock_available = threading.Event()

    def acquire_lock():
        with exif_utils._pillow_metadata_read():
            lock_available.set()

    def extract(path, *, scan_jpeg=False):
        assert scan_jpeg is True
        thread = threading.Thread(target=acquire_lock, daemon=True)
        thread.start()
        assert lock_available.wait(2)
        thread.join(timeout=2)
        with open(path, 'rb') as source:
            assert source.read() == data
        calls.append(path)
        return {'FileType': 'JPEG', 'ImageWidth': '40', 'ImageHeight': '20'}

    monkeypatch.setattr(Image.Image, 'load', fail)
    monkeypatch.setattr(ImageFile.ImageFile, 'load', fail)
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', extract)
    assert exif_utils.extract_exif_from_bytes(data, 'photo.jpg')['FileType'] == 'JPEG'
    assert len(calls) == 1


def test_full_jpeg_scan_is_opt_in_for_originals(monkeypatch):
    calls = _tool_result(monkeypatch, b'[{"File:FileType": "JPEG"}]')
    assert exif_utils._extract_exiftool_from_path('source.jpg')['FileType'] == 'JPEG'
    assert calls[-1][0] == ['/test/exiftool', '-j', '-G1', '-n', 'source.jpg']
    assert exif_utils._extract_exiftool_from_path('source.jpg', scan_jpeg=True)['FileType'] == 'JPEG'
    assert calls[-1][0] == ['/test/exiftool', '-j', '-G1', '-n', '-ee', 'source.jpg']


def _jpeg_with_late_exif():
    exif = Image.Exif()
    exif[272] = 'Late JPEG Camera'
    exif[0x8825] = {1: 'N', 2: (55.0, 36.0, 0.0), 3: 'E', 4: (13.0, 0.0, 0.0)}
    blob = exif.tobytes()
    app1 = b'\xff\xe1' + (len(blob) + 2).to_bytes(2, 'big') + blob
    data = _raster_bytes()
    assert data.endswith(b'\xff\xd9') and b'\xff\xda' in data
    # Retain the entire valid scan; insert APP1 at the entropy/EOI boundary.
    return data[:-2] + app1 + data[-2:]


def test_late_exif_fixture_preserves_decodable_jpeg_pixels():
    data = _jpeg_with_late_exif()
    app1 = data.index(b'\xff\xe1')
    app1_length = int.from_bytes(data[app1 + 2:app1 + 4], 'big')
    assert data.index(b'\xff\xda') < app1
    assert app1 + 2 + app1_length == len(data) - 2
    # Fixture validation decodes pixels; the extraction tests below forbid it.
    with Image.open(io.BytesIO(_raster_bytes())) as original, Image.open(io.BytesIO(data)) as late:
        original.load()
        late.load()
        assert late.size == original.size
        assert late.tobytes() == original.tobytes()


@pytest.mark.skipif(shutil.which('exiftool') is None, reason='ExifTool is not installed')
def test_real_exiftool_default_skips_late_app1_but_extract_embedded_reads_it(tmp_path):
    path = tmp_path / 'late.jpg'
    path.write_bytes(_jpeg_with_late_exif())
    records = []
    for flags in ([], ['-ee']):
        result = subprocess.run(
            [shutil.which('exiftool'), '-j', '-a', '-G1', '-n', *flags,
             '-Warning', '-Error', '-Model', '-GPSLatitude', '-GPSLongitude', str(path)],
            capture_output=True, timeout=exif_utils.EXIFTOOL_TIMEOUT_SECONDS, check=False)
        assert result.returncode == 0
        assert not result.stderr
        record = json.loads(result.stdout)[0]
        assert not any(key.rsplit(':', 1)[-1] in {'Warning', 'Error'} for key in record)
        records.append(record)
    assert 'IFD0:Model' not in records[0]
    assert 'GPS:GPSLatitude' not in records[0]
    assert 'GPS:GPSLongitude' not in records[0]
    assert records[1]['IFD0:Model'] == 'Late JPEG Camera'
    assert records[1]['GPS:GPSLatitude'] == 55.6
    assert records[1]['GPS:GPSLongitude'] == 13


@pytest.mark.skipif(shutil.which('exiftool') is None, reason='ExifTool is not installed')
@pytest.mark.parametrize('trailer', [False, True])
def test_full_jpeg_extraction_finds_late_exif_without_decoding(monkeypatch, tmp_path, trailer):
    data = _jpeg_with_late_exif() if trailer else _raster_bytes()
    path = tmp_path / 'source.jpg'
    path.write_bytes(data)
    with Image.open(io.BytesIO(data)) as image:
        assert not image.getexif()
        image.verify()

    def fail(*args, **kwargs):
        pytest.fail('EXIF extraction must not decode pixels')

    monkeypatch.setattr(Image.Image, 'load', fail)
    monkeypatch.setattr(ImageFile.ImageFile, 'load', fail)
    for result in (exif_utils.extract_exif_from_bytes(data, path.name),
                   exif_utils.extract_exif_from_path(str(path))):
        assert result['FileType'] == 'JPEG'
        if trailer:
            assert result['Model'] == 'Late JPEG Camera'
            assert ipwork_geo.geo_coordinates_from_exif(result) == (55.6, 13.0)
        else:
            assert 'Model' not in result
            assert 'GPS.LatitudeDecimal' not in result
    processed = ipwork_geo.process_exif('u', path.name, data)
    assert processed['hasData'] is True
    assert 'verifiedNoExif' not in processed
    if trailer:
        assert processed['data']['Model'] == 'Late JPEG Camera'


def _png_with_trailer_exif():
    exif = Image.Exif()
    exif[272] = 'Trailer Camera'
    exif[0x8769] = {36867: '2020:01:02 03:04:05'}
    data = _raster_bytes('PNG', exif=exif)
    chunks, exif_chunk, offset = [], None, 8
    while offset < len(data):
        length = int.from_bytes(data[offset:offset + 4], 'big')
        chunk = data[offset:offset + length + 12]
        if chunk[4:8] == b'eXIf':
            exif_chunk = chunk
        else:
            chunks.append(chunk)
        offset += length + 12
    assert exif_chunk is not None and chunks[-1][4:8] == b'IEND'
    assert any(chunk[4:8] == b'IDAT' for chunk in chunks[:-1])
    return data[:8] + b''.join(chunks[:-1]) + exif_chunk + chunks[-1]


@pytest.mark.parametrize('trailer', [False, True])
def test_png_uses_complete_original_outside_pillow_lock_without_decoding(monkeypatch, trailer):
    data = _png_with_trailer_exif() if trailer else _raster_bytes('PNG')
    calls = []

    def fail(*args, **kwargs):
        pytest.fail('PNG must not open or decode pixels through Pillow')

    class RejectLock:
        def __enter__(self):
            pytest.fail('PNG ExifTool extraction must run outside the Pillow lock')

        def __exit__(self, *args):
            pass

    def extract(path):
        with open(path, 'rb') as source:
            assert source.read() == data
        calls.append(path)
        return {'FileType': 'PNG', **({'Model': 'Trailer Camera'} if trailer else {})}

    monkeypatch.setattr(Image, 'open', fail)
    monkeypatch.setattr(Image.Image, 'load', fail)
    monkeypatch.setattr(ImageFile.ImageFile, 'load', fail)
    monkeypatch.setattr(PngImagePlugin.PngImageFile, 'load', fail)
    monkeypatch.setattr(exif_utils, '_PILLOW_METADATA_LOCK', RejectLock())
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', extract)
    # Detect by signature even if the caller supplies no name or the wrong suffix.
    result = exif_utils.extract_exif_from_bytes(data, 'misnamed.jpg')
    assert result['FileType'] == 'PNG'
    assert ('Model' in result) is trailer
    assert len(calls) == 1


@pytest.mark.parametrize('entrypoint', ['bytes', 'path', 'image'])
def test_png_without_exiftool_never_decodes_or_claims_absence(monkeypatch, tmp_path, entrypoint):
    data = _raster_bytes('PNG')
    path = tmp_path / 'plain.png'
    path.write_bytes(data)
    monkeypatch.setattr(exif_utils.shutil, 'which', lambda _: None)

    def fail(*args, **kwargs):
        pytest.fail('PNG metadata extraction must not decode pixels')

    monkeypatch.setattr(PngImagePlugin.PngImageFile, 'load', fail)
    with pytest.raises(exif_utils.ExifExtractionError) as caught:
        if entrypoint == 'bytes':
            exif_utils.extract_exif_from_bytes(data, path.name)
        elif entrypoint == 'path':
            exif_utils.extract_exif_from_path(str(path))
        else:
            with Image.open(io.BytesIO(data)) as image:
                exif_utils.extract_exif_from_image(image)
    assert caught.value.category == ('read_failed' if entrypoint == 'image' else 'unavailable')


@pytest.mark.skipif(shutil.which('exiftool') is None, reason='ExifTool is not installed')
@pytest.mark.parametrize('trailer', [False, True])
def test_real_png_extraction_preserves_trailer_exif_without_pixel_decoding(monkeypatch, tmp_path, trailer):
    data = _png_with_trailer_exif() if trailer else _raster_bytes('PNG')
    path = tmp_path / 'source.png'
    path.write_bytes(data)

    def fail(*args, **kwargs):
        pytest.fail('PNG metadata extraction must not decode pixels')

    monkeypatch.setattr(PngImagePlugin.PngImageFile, 'load', fail)
    monkeypatch.setattr(ImageFile.ImageFile, 'load', fail)
    for result in (exif_utils.extract_exif_from_bytes(data, path.name),
                   exif_utils.extract_exif_from_path(str(path))):
        assert result['FileType'] == 'PNG'
        assert result['ImageWidth'] == '40'
        if trailer:
            assert result['Model'] == 'Trailer Camera'
            assert result['DateTimeOriginal'] == '2020:01:02 03:04:05'
        else:
            assert 'Model' not in result
            assert 'DateTimeOriginal' not in result


def test_empty_input_is_read_failure():
    with pytest.raises(exif_utils.ExifExtractionError, match='read_failed'):
        exif_utils.extract_exif_from_bytes(b'')


def test_temporary_file_failure_is_read_failure(monkeypatch):
    def fail(**kwargs):
        raise OSError('private temp directory')

    monkeypatch.setattr(exif_utils.tempfile, 'NamedTemporaryFile', fail)
    with pytest.raises(exif_utils.ExifExtractionError, match='read_failed'):
        exif_utils.extract_exif_from_bytes(b'unsupported container')


@pytest.mark.parametrize('recovered', [True, False])
def test_corrupt_jpeg_metadata_uses_complete_tool_extraction(monkeypatch, recovered):
    data = _raster_bytes(exif=b'Exif\x00\x00II*\x00\x08\x00\x00\x00\x01\x00')
    output = (b'[{"IFD0:Make": "Recovered camera"}]' if recovered
              else b'[{"ExifTool:Error": "Invalid metadata"}]')
    calls = _tool_result(monkeypatch, output)
    result = ipwork_geo.process_exif('u', 'corrupt.jpg', data)
    assert len(calls) == 1
    if recovered:
        assert result == {'hasData': True, 'data': {'Make': 'Recovered camera'}}
    else:
        assert result == {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_parser_error'}


@pytest.mark.parametrize('phase', ['open', 'getexif'])
def test_broad_pillow_parse_exception_uses_tool(monkeypatch, phase):
    def fail(*args, **kwargs):
        raise RuntimeError('Pillow parser failed')

    monkeypatch.setattr(Image if phase == 'open' else Image.Image,
                        'open' if phase == 'open' else 'getexif', fail)
    calls = _tool_result(monkeypatch, b'[{"IFD0:Make": "Recovered camera"}]')
    assert exif_utils.extract_exif_from_bytes(_raster_bytes('TIFF'), 'photo.tiff') == {'Make': 'Recovered camera'}
    assert len(calls) == 1


@pytest.mark.parametrize('tag', [0x8769, 0x8825])
def test_readable_tiff_with_unreadable_referenced_ifd_uses_tool(monkeypatch, tag):
    exif = Image.Exif()
    exif[tag] = {36867: '2020:01:02 03:04:05'} if tag == 0x8769 else {1: 'N', 2: (55.0, 0.0, 0.0)}
    data = bytearray(_raster_bytes('TIFF', exif=exif.tobytes()))
    byte_order = '<' if data[:2] == b'II' else '>'
    offset = struct.unpack_from(byte_order + 'I', data, 4)[0]
    count = struct.unpack_from(byte_order + 'H', data, offset)[0]
    for index in range(count):
        entry = offset + 2 + 12 * index
        if struct.unpack_from(byte_order + 'H', data, entry)[0] == tag:
            struct.pack_into(byte_order + 'I', data, entry + 8, len(data) + 1024)
            break
    else:
        pytest.fail('Fixture lacks a referenced IFD')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    calls = _tool_result(monkeypatch, b'[{"ExifTool:Error": "Invalid IFD offset"}]')
    with pytest.warns(UserWarning):
        result = ipwork_geo.process_exif('u', 'corrupt.tiff', bytes(data))
    assert len(calls) == 1
    assert result['errorCode'] == 'exif_parser_error'


def test_referenced_ifd_exception_is_not_partial_metadata_success(monkeypatch):
    exif = Image.Exif()
    exif[0x8769] = {36867: '2020:01:02 03:04:05'}
    data = _raster_bytes(exif=exif)

    def fail(*args):
        raise RuntimeError('Unreadable SubIFD')

    monkeypatch.setattr(Image.Exif, 'get_ifd', fail)
    calls = _tool_result(monkeypatch, b'[{"IFD0:Make": "Recovered camera"}]')
    assert exif_utils.extract_exif_from_bytes(data, 'photo.jpg') == {'Make': 'Recovered camera'}
    assert len(calls) == 1


def test_pillow_metadata_warning_capture_serializes_exif_readers():
    first_active, second_started, second_active, release = (threading.Event() for _ in range(4))

    def first():
        with exif_utils._pillow_metadata_read():
            first_active.set()
            assert release.wait(2)
            warnings.warn('Corrupt EXIF data', UserWarning)

    def second():
        second_started.set()
        with exif_utils._pillow_metadata_read():
            second_active.set()
        return {}

    with pytest.warns(UserWarning, match='Corrupt EXIF data'), ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(first)
        try:
            assert first_active.wait(2)
            clean = pool.submit(second)
            assert second_started.wait(2)
            assert not second_active.wait(0.05)
        finally:
            release.set()
        with pytest.raises(exif_utils.ExifExtractionError, match='read_failed'):
            failed.result(timeout=2)
        assert clean.result(timeout=2) == {}


def test_foreign_thread_warning_cannot_fail_clean_metadata_read():
    active, release = threading.Event(), threading.Event()

    def parse():
        with exif_utils._pillow_metadata_read():
            active.set()
            assert release.wait(2)
        return {}

    with pytest.warns(UserWarning, match='Foreign image warning'), ThreadPoolExecutor(max_workers=1) as pool:
        clean = pool.submit(parse)
        try:
            assert active.wait(2)
            warnings.warn('Foreign image warning', UserWarning)
        finally:
            release.set()
        assert clean.result(timeout=2) == {}


def test_pillow_success_keeps_normalized_metadata_and_skips_tool(monkeypatch):
    exif = Image.Exif()
    exif[271], exif[272], exif[274] = 'Camera Maker', 'Camera Model', 6
    data = _raster_bytes(exif=exif)
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda _: pytest.fail('Unexpected ExifTool call'))
    with Image.open(io.BytesIO(data)) as image:
        assert exif_utils.extract_exif_from_image(image) == {
            'Make': 'Camera Maker', 'Model': 'Camera Model', 'Orientation': '6'}


def test_exiftool_success_preserves_warning_and_normalization(monkeypatch):
    raw = {'SourceFile': 'private/path', 'System:FileName': 'photo.dng',
           'IFD0:Make': 'Camera', 'ExifIFD:ISO': 100,
           'GPS:GPSLatitude': 55.6, 'GPS:GPSLongitude': 13,
           'ExifTool:Warning': 'Known warning'}
    _tool_result(monkeypatch, json.dumps([raw]).encode())
    expected = {'Make': 'Camera', 'ISO': '100', 'GPS.GPSLatitude': '55.6',
                'GPS.GPSLongitude': '13', 'Warning': 'Known warning',
                'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '13.0'}
    assert exif_utils.extract_exif_from_bytes(b'unsupported container', 'photo.dng') == expected


@pytest.mark.parametrize('category', sorted(exif_utils.ExifExtractionError.CATEGORIES))
def test_processors_propagate_bounded_extraction_failure(monkeypatch, category):
    def fail(*args):
        raise exif_utils.ExifExtractionError(category)

    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', fail)
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Extraction failed'))
    expected = {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_' + category}
    assert ipwork_geo.process_exif('u', 'photo.dng', b'data') == expected
    assert ipwork_geo.process_geo('u', 'photo.dng', b'data') == expected


@pytest.mark.parametrize('data', [{'Error': 'private diagnostic'}, {'Error': '', 'Make': 'Camera'}])
def test_processor_rejects_error_dictionary(monkeypatch, data):
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: data)
    expected = {'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_parser_error'}
    assert ipwork_geo.process_exif('u', 'photo.dng', b'data') == expected
    assert ipwork_geo.process_geo('u', 'photo.dng', b'data') == expected


def test_unknown_processor_exception_remains_exception(monkeypatch):
    def fail(*args):
        raise RuntimeError('unexpected implementation failure')

    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', fail)
    for processor in (ipwork_geo.process_exif, ipwork_geo.process_geo):
        with pytest.raises(RuntimeError, match='unexpected implementation failure'):
            processor('u', 'photo.dng', b'data')


def test_unknown_exiftool_exception_remains_exception(monkeypatch):
    _tool_result(monkeypatch, b'')

    def fail(*args, **kwargs):
        raise RuntimeError('unexpected implementation failure')

    monkeypatch.setattr(exif_utils.subprocess, 'run', fail)
    with pytest.raises(RuntimeError, match='unexpected implementation failure'):
        exif_utils.extract_exif_from_bytes(b'unsupported container', 'photo.dng')


def test_error_category_cannot_include_arbitrary_diagnostics():
    with pytest.raises(ValueError, match='^Unknown EXIF extraction category$'):
        exif_utils.ExifExtractionError('private unbounded diagnostic')


@pytest.mark.parametrize('value', ['nan', 'NaN', 'inf', '-inf', float('nan'), float('inf')])
def test_exiftool_coordinates_reject_nonfinite_values(value):
    assert exif_utils._valid_decimal_from_exiftool(value, 'N', -90, 90) == ''


@pytest.mark.parametrize('data', [
    {}, {'Make': 'Camera'}, {'GPS.GPSAltitude': '10', 'GPS.GPSAltitudeRef': '0'},
])
def test_truly_absent_coordinates_are_no_data(monkeypatch, data):
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('No coordinates'))
    assert ipwork_geo.geo_coordinates_from_exif(data) is None
    assert ipwork_geo.process_geo_from_exif(data) == {'hasData': False}


@pytest.mark.parametrize('data', [
    {'GPS.LatitudeDecimal': '55.6'},
    {'GPS.LongitudeDecimal': '13'},
    {'GPS.LatitudeDecimal': '', 'GPS.LongitudeDecimal': ''},
    {'GPS.GPSLatitude': 'broken DMS', 'GPS.GPSLongitude': '13'},
    {'GPS.GPSLatitude': '((55, 0), (0, 1), (0, 1))', 'GPS.GPSLongitude': '13'},
    {'GPS.GPSLatitude': '((55, 1), (0, 1))', 'GPS.GPSLongitude': '13'},
    {'GPSLatitude': '55, 0, 0'},
    {'GPS.Latitude': '55.6', 'GPS.Longitude': 'not a coordinate'},
    {'GPS.LatitudeDecimal': 'NaN', 'GPS.LongitudeDecimal': '13'},
    {'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': 'inf'},
    {'GPS.LatitudeDecimal': '91', 'GPS.LongitudeDecimal': '13'},
    {'GPS.LatitudeDecimal': '55.6', 'GPS.LongitudeDecimal': '-181'},
])
def test_partial_or_malformed_coordinates_raise_and_propagate(monkeypatch, data):
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: data)
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Invalid coordinates'))
    with pytest.raises(ValueError, match='Invalid GPS coordinates'):
        ipwork_geo.geo_coordinates_from_exif(data)
    with pytest.raises(ValueError, match='Invalid GPS coordinates'):
        ipwork_geo.process_geo_from_exif(data)
    with pytest.raises(ValueError, match='Invalid GPS coordinates'):
        ipwork_geo.process_geo('u', 'photo.jpg', b'data')


@pytest.mark.parametrize('latitude,longitude', [('0', '0'), ('-90', '180'), ('90', '-180')])
def test_valid_zero_and_boundary_coordinates_still_geocode(monkeypatch, latitude, longitude):
    calls = []
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode',
                        lambda *coords: calls.append(coords) or {'city': 'Test City'})
    result = ipwork_geo.process_geo_from_exif({'GPS.LatitudeDecimal': latitude, 'GPS.LongitudeDecimal': longitude})
    assert calls == [(float(latitude), float(longitude))]
    assert result['hasData'] is True
    assert result['city'] == 'Test City'


@pytest.mark.parametrize('latitude,longitude', [('0', '0'), ('-90', '180'), ('90', '-180'), ('55.6', '13')])
def test_shared_coordinate_validator_never_geocodes(monkeypatch, latitude, longitude):
    monkeypatch.setattr(ipwork_geo.maps_utils, 'reverse_geocode', lambda *_: pytest.fail('Pure validation must not geocode'))
    assert ipwork_geo.geo_coordinates_from_exif({
        'GPS.LatitudeDecimal': latitude, 'GPS.LongitudeDecimal': longitude}) == (float(latitude), float(longitude))


def test_malformed_gps_becomes_worker_failure(monkeypatch):
    import app

    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', False)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {})
    monkeypatch.setattr(app, 'download_media_bytes', lambda *_: b'data')
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'map_detection', ipwork_geo.process_geo)
    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', lambda *_: {'GPS.LatitudeDecimal': '55.6'})
    assert app._run_ipwork_steps('u', 'photo.jpg', ['map_detection']) == {
        'map_detection': {'hasData': False, 'error': 'processing_failed'}}


@pytest.mark.parametrize('known', [True, False])
def test_worker_preserves_typed_failure_and_generic_unknown_failure(monkeypatch, known):
    import app

    monkeypatch.setattr(app, 'IPWORK_REUSE_METADATA_PACKET', False)
    monkeypatch.setattr(app, 'IPWORK_REUSE_EXIF', False)
    monkeypatch.setattr(app, '_get_metadata_entity', lambda *_: {})
    monkeypatch.setattr(app, 'download_media_bytes', lambda *_: b'unsupported container')
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'exif', ipwork_geo.process_exif)
    monkeypatch.setitem(app.IPWORK_STEP_PROCESSORS, 'map_detection', ipwork_geo.process_geo)

    def fail(*args):
        if known:
            raise exif_utils.ExifExtractionError('timeout')
        raise RuntimeError('unexpected implementation failure')

    monkeypatch.setattr(ipwork_geo, 'extract_exif_from_bytes', fail)
    expected = ({'hasData': False, 'error': 'exif_extraction_failed', 'errorCode': 'exif_timeout'}
                if known else {'hasData': False, 'error': 'processing_failed'})
    assert app._run_ipwork_steps('u', 'photo.dng', ['exif', 'map_detection']) == {
        'exif': expected, 'map_detection': expected}


@pytest.mark.parametrize('warnings,expected,allowed', [
    (['A', 'B'], ['B', 'A'], True),
    (['A', 'A', 'B'], ['A', 'B', 'A'], True),
    (['A', 'A', 'B'], ['A', 'B'], False),
    (['A', 'B'], ['A', 'A', 'B'], False),
    (['A', 'C'], ['A', 'B'], False),
    (['A'], ['a'], False),
    ([], ['A'], False),
    ([], [], False),
])
def test_exact_warning_multiset(monkeypatch, warnings, expected, allowed):
    tags = {'SourceFile': 'photo.fff', **{f'Copy{i}:Warning': value for i, value in enumerate(warnings)}}
    calls = _tool_result(monkeypatch, json.dumps([tags]).encode())
    assert exif_utils._exiftool_has_only_warnings('photo.fff', iter(expected)) is allowed
    if expected:
        assert calls[0][0] == ['/test/exiftool', '-j', '-G4', '-Warning', '-Error', 'photo.fff']
        assert calls[0][1]['timeout'] == exif_utils.EXIFTOOL_TIMEOUT_SECONDS


@pytest.mark.parametrize('stdout', [
    b'not JSON', b'\xff', b'{}', b'[]', b'[null]', b'[{}, {}]',
    b'[{"Warning": "A", "Error": "private error"}]',
    b'[{"Warning": "A", "Copy1:Error": "private error"}]',
    b'[{"Warning": "A", "stdoutError": "private error"}]',
    b'[{"Warning": "A", "Make": "Camera"}]',
    b'[{"Warning": ["A"]}]',
])
def test_warning_multiset_rejects_errors_unexpected_tags_and_parse_errors(monkeypatch, stdout):
    _tool_result(monkeypatch, stdout)
    assert not exif_utils._exiftool_has_only_warnings('photo.fff', ['A'])


@pytest.mark.parametrize('failure', ['stderr', 'nonzero', 'unavailable', 'timeout', 'execution'])
def test_warning_multiset_rejects_tool_failures(monkeypatch, failure):
    _tool_result(monkeypatch, b'[{"Warning": "A"}]',
                 returncode=1 if failure == 'nonzero' else 0,
                 stderr=b'private error' if failure == 'stderr' else b'')
    if failure == 'unavailable':
        monkeypatch.setattr(exif_utils.shutil, 'which', lambda _: None)
    elif failure in {'timeout', 'execution'}:
        def fail(*args, **kwargs):
            if failure == 'timeout':
                raise subprocess.TimeoutExpired('private command', 1)
            raise OSError('private error')

        monkeypatch.setattr(exif_utils.subprocess, 'run', fail)
    assert not exif_utils._exiftool_has_only_warnings('photo.fff', ['A'])


@pytest.mark.parametrize('expected', ['A', b'A', None, [1], []])
def test_warning_multiset_requires_nonempty_iterable_of_strings(monkeypatch, expected):
    _tool_result(monkeypatch, b'[{"Warning": "A"}]')
    assert not exif_utils._exiftool_has_only_warnings('photo.fff', expected)


def test_singular_warning_helper_keeps_repeat_tolerant_semantics(monkeypatch):
    _tool_result(monkeypatch, b'[{"Warning": "A", "Copy1:Warning": "A"}]')
    assert exif_utils._exiftool_has_only_warning('photo.dng', 'A')
    assert not exif_utils._exiftool_has_only_warnings('photo.dng', ['A'])
