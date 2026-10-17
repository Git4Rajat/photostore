import shutil
import io
import struct

import pytest

import exif_utils
import upload_metadata as transport
from tests.test_exif_outcomes import _jpeg_with_late_exif
from tests.test_upload_metadata import _jpeg_candidate, _jpeg_exif
from PIL import Image


def test_header_only_camera_packet_is_not_complete_metadata(monkeypatch):
    _, _, ranges, source = _jpeg_candidate(source=_jpeg_with_late_exif())
    storage, row, _, _ = _jpeg_candidate(ranges=ranges[:1], source=source)
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda *_a, **_k: pytest.fail('Incomplete packet'))
    outcomes = []
    assert transport.extract_candidate(storage, 'camera.jpg', row, on_outcome=outcomes.append) is None
    assert outcomes == ['incomplete_jpeg_metadata']


@pytest.mark.skipif(shutil.which('exiftool') is None, reason='ExifTool is not installed')
def test_complete_packet_preserves_exif_after_scan(tmp_path):
    source = _jpeg_with_late_exif()
    path = tmp_path / 'source.jpg'
    path.write_bytes(source)
    storage, row, _, _ = _jpeg_candidate(source=source)
    result = transport.extract_candidate(storage, 'camera.jpg', row)
    expected = exif_utils._extract_exiftool_from_path(str(path), scan_jpeg=True)
    for key in ('FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'):
        expected.pop(key, None)
    assert result == expected
    assert result['Model'] == 'Late JPEG Camera'
    assert result['GPS.LatitudeDecimal'] and result['GPS.LongitudeDecimal']


def seft_source():
    _, _, _, source = _jpeg_candidate()
    block = b'\0' * 4 + (4).to_bytes(4, 'little') + b'Test' + b'value'
    directory = b'SEFH' + b'\0' * 4 + (1).to_bytes(4, 'little')
    directory += b'\0' * 4 + len(block).to_bytes(4, 'little') * 2
    return source + block + directory + len(directory).to_bytes(4, 'little') + b'SEFT'


@pytest.mark.parametrize('fault', [None, 'footer', 'gap', 'pointer', 'length'])
def test_seft_trailer_needs_complete_retained_structure(fault):
    source = bytearray(seft_source())
    if fault == 'footer':
        source[-4:] = b'bad!'
    elif fault == 'pointer':
        source[-16:-12] = (0xffffffff).to_bytes(4, 'little')
    elif fault == 'length':
        source[-8:-4] = (0xffffffff).to_bytes(4, 'little')
    storage, row, ranges, _ = _jpeg_candidate(source=bytes(source))
    if fault == 'gap':
        ranges = [(offset, data[:-1]) if index == len(ranges) - 1 else (offset, data)
                  for index, (offset, data) in enumerate(ranges)]
    assert transport._verified_jpeg_structure('camera.jpg', ranges, len(source), _jpeg_exif(), metadata=True) is (fault is None)


def mpf_source(little):
    order = '<' if little else '>'
    primary = io.BytesIO()
    exif = Image.Exif()
    exif[271], exif[272] = 'Camera', 'Primary Camera'
    Image.new('RGB', (40, 20), 'red').save(primary, 'JPEG', exif=exif)
    secondary = io.BytesIO()
    Image.new('RGB', (20, 10), 'blue').save(secondary, 'JPEG')
    image = primary.getvalue()
    body_size = 4 + 8 + 2 + 3 * 12 + 4 + 32
    first_size = len(image) + body_size + 4
    tiff = (b'II' if little else b'MM') + struct.pack(order + 'HIH', 42, 8, 3)
    tiff += struct.pack(order + 'HHI', 0xb000, 7, 4) + b'0100'
    tiff += struct.pack(order + 'HHII', 0xb001, 4, 1, 2)
    tiff += struct.pack(order + 'HHII', 0xb002, 7, 32, 50)
    tiff += struct.pack(order + 'I', 0)
    tiff += struct.pack(order + 'IIIHH', 0x20030000, first_size, 0, 0, 0)
    tiff += struct.pack(order + 'IIIHH', 0, len(secondary.getvalue()), first_size - 10, 0, 0)
    body = b'MPF\0' + tiff
    source = image[:2] + b'\xff\xe2' + (len(body) + 2).to_bytes(2, 'big') + body + image[2:]
    _, _, first_ranges, _ = _jpeg_candidate(source=source)
    _, _, child_ranges, _ = _jpeg_candidate(source=secondary.getvalue())
    ranges = first_ranges + [(offset + len(source), data) for offset, data in child_ranges]
    return source + secondary.getvalue(), ranges


@pytest.mark.parametrize('little', [False, True])
def test_mpf_requires_complete_metadata_for_each_constituent_jpeg(little, monkeypatch):
    source, ranges = mpf_source(little)
    assert transport._verified_jpeg_structure('camera.jpg', ranges, len(source), _jpeg_exif(), metadata=True)
    assert not transport._verified_jpeg_structure('camera.jpg', ranges[:-1], len(source), _jpeg_exif(), metadata=True)
    assert not transport._verified_jpeg_no_exif('camera.jpg', ranges, len(source), _jpeg_exif())
    storage, row, _, _ = _jpeg_candidate(source=source, ranges=ranges[:2])
    monkeypatch.setattr(exif_utils, '_extract_exiftool_from_path', lambda *_, **__: pytest.fail('Missing child metadata'))
    outcomes = []
    assert transport.extract_candidate(storage, 'camera.jpg', row, on_outcome=outcomes.append) is None
    assert outcomes == ['incomplete_jpeg_metadata']


@pytest.mark.skipif(shutil.which('exiftool') is None, reason='ExifTool is not installed')
@pytest.mark.parametrize('little', [False, True])
def test_mpf_packet_has_exact_original_extraction_parity(tmp_path, little):
    source, ranges = mpf_source(little)
    storage, row, _, _ = _jpeg_candidate(source=source, ranges=ranges)
    path = tmp_path / 'compound.jpg'
    path.write_bytes(source)
    result = transport.extract_candidate(storage, 'camera.jpg', row)
    expected = exif_utils._extract_exiftool_from_path(str(path), scan_jpeg=True)
    for key in ('FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'):
        expected.pop(key, None)
    assert result == expected


@pytest.mark.parametrize('fault', ['count', 'length', 'offset', 'size', 'non-jpeg', 'cycle'])
def test_mpf_rejects_unproven_image_tables(fault):
    source, ranges = mpf_source(True)
    modified = bytearray(ranges[0][1])
    if fault == 'count':
        struct.pack_into('<I', modified, 10 + 8 + 2 + 12 + 8, 33)
    elif fault == 'length':
        struct.pack_into('<I', modified, 10 + 8 + 2 + 24 + 4, 16)
    elif fault == 'offset':
        struct.pack_into('<I', modified, 10 + 50 + 16 + 8, len(source))
    elif fault == 'size':
        struct.pack_into('<I', modified, 10 + 50 + 16 + 4, len(source))
    elif fault == 'non-jpeg':
        struct.pack_into('<I', modified, 10 + 50 + 16, 0x01000000)
    else:
        struct.pack_into('<I', modified, 10 + 50 + 16 + 8, 0)
    ranges[0] = (0, bytes(modified))
    assert not transport._verified_jpeg_structure('camera.jpg', ranges, len(source), _jpeg_exif(), metadata=True)
