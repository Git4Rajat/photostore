import json
from pathlib import Path
import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest

import metadata_tiff_coverage as coverage


PROFILES = [
    ('sample_004.srw', 'SAMSUNG', 'NX mini', 'SRW'),
    ('sample_017.srw', 'SAMSUNG', 'NX3000', 'SRW'),
    ('sample_021.srw', 'SAMSUNG', 'NX3300', 'SRW'),
    ('sample_001.kdc', 'EASTMAN KODAK COMPANY', 'KODAK EasyShare Z981 Digital Camera', 'KDC'),
    ('sample_002.kdc', 'EASTMAN KODAK COMPANY', 'KODAK EasyShare Z990 Digital Camera', 'KDC'),
    ('sample_003.kdc', 'EASTMAN KODAK COMPANY', 'KODAK P712 ZOOM DIGITAL CAMERA', 'KDC'),
    ('sample_004.kdc', 'EASTMAN KODAK COMPANY', 'KODAK P880 ZOOM DIGITAL CAMERA', 'KDC'),
    ('sample_005.kdc', 'EASTMAN KODAK COMPANY', 'KODAK EASYSHARE Z1015 IS DIGITAL CAMERA', 'KDC'),
]
PRIVATE = Path(__file__).resolve().parents[2] / '.azure/forenklaprep-20261015'


def _exif(profile):
    return dict(zip(('Make', 'Model', 'FileType'), profile[1:]))


def _fixture(profile, order='<'):
    """Sparse metadata with an omitted sensor region and omitted JPEG scan."""
    source = bytearray(16384)
    spans = []

    def put(offset, data):
        source[offset:offset + len(data)] = data
        spans.append((offset, offset + len(data)))

    def ifd(offset, entries, base_order=order, next_link=True):
        put(offset, struct.pack(base_order + 'H', len(entries))
            + b''.join(struct.pack(base_order + 'HHII', *entry) for entry in entries)
            + (bytes(4) if next_link else b''))

    make, model, kind = profile[1:]
    make_bytes, model_bytes = make.encode() + b'\0', model.encode() + b'\0'
    put(0, (b'II' if order == '<' else b'MM') + struct.pack(order + 'HI', 42, 8))
    root = [(0x010f, 2, len(make_bytes), 300), (0x0110, 2, len(model_bytes), 350),
            (0x8769, 4, 1, 400), (0x0111, 4, 1, 4096), (0x0117, 4, 1, 4096)]
    put(300, make_bytes)
    put(350, model_bytes)
    pixels = [(4096, 8192)]
    if kind == 'SRW':
        ifd(400, [(0x927c, 7, 256, 512)])
        put(512, bytes(256))
        ifd(512, [(1, 7, 4, int.from_bytes(b'0100', 'little')), (0x0035, 4, 1, 256)], '<', False)
        header = (b'\xff\xd8\xff\xe1\x00\x08Exif\0\0'
                  b'\xff\xc0\x00\x0b\x08\x00\x14\x00\x28\x01\x01\x11\x00'
                  b'\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00')
        ifd(768, [(0x011a, 5, 1, 600), (0x011b, 5, 1, 608),
                  (0x0201, 4, 1, 1000), (0x0202, 4, 1, len(header) + 1002)], '<')
        put(1112, struct.pack('<IIII', 72, 1, 96, 1))
        put(1512, header)
        pixels.append((1512 + len(header), 1512 + len(header) + 1000))
    else:
        root.append((0xfe00, 4, 1, 2048))
        if profile[2] in coverage._KDC_LEGACY:
            ifd(400, [(0x927c, 7, 64, 512), (0xa005, 4, 1, 314)])
            put(512, bytes(64))
            ifd(512, [(0xff00, 4, 1, 768), (0x0f00, 7, 16, 1000)], next_link=False)
            scalar = 3000 if order == '<' else 3000 << 16
            ifd(768, [(0xf900, 3, 1, scalar), (0xf901, 3, 1, scalar),
                      (0xfa04, 2, 32, 1100)], next_link=False)
            put(1000, b'opaque metadata!')
            put(1100, b'camera serial'.ljust(32, b'\0'))
        else:
            ifd(400, [])
        ifd(2048, [(0xfa00, 2, 32, 2200), (0xfa0d, 1, 1, 0)]
            + [(tag, 4, 3, 2300 + index * 12)
               for index, tag in enumerate((0xfa25, 0xfa27, 0xfa28, 0xfa29, 0xfa2a))])
        put(2200, b'KDC serial'.ljust(32, b'\0'))
        put(2300, struct.pack(order + 'I' * 15, *range(65536, 65551)))
    ifd(8, root)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return [(start, bytes(source[start:end])) for start, end in merged], len(source), pixels


def _remove(ranges, offset):
    result = []
    found = False
    for start, data in ranges:
        if start <= offset < start + len(data):
            found = True
            pos = offset - start
            if pos:
                result.append((start, data[:pos]))
            if pos + 1 < len(data):
                result.append((offset + 1, data[pos + 1:]))
        else:
            result.append((start, data))
    assert found
    return result


def _replace(ranges, offset, data):
    result = []
    found = False
    for start, original in ranges:
        if start <= offset and offset + len(data) <= start + len(original):
            found = True
            pos = offset - start
            original = original[:pos] + data + original[pos + len(data):]
        result.append((start, original))
    assert found
    return result


@pytest.mark.parametrize('profile', PROFILES, ids=lambda profile: profile[0])
def test_required_profiles_inspect_only_metadata_and_allow_adjacent_ranges(monkeypatch, profile):
    ranges, size, pixels = _fixture(profile)
    reads = []
    original = coverage._Ranges.read

    def read(reader, offset, length):
        reads.append((offset, length))
        return original(reader, offset, length)

    monkeypatch.setattr(coverage._Ranges, 'read', read)
    # Split even TIFF words across adjacent packet ranges.
    split = [(start + pos, data[pos:pos + 3]) for start, data in ranges for pos in range(0, len(data), 3)]
    assert coverage.verified_camera_ranges('renamed.unrelated', _exif(profile), split, size)
    assert all(offset + length <= first or offset >= last
               for offset, length in reads for first, last in pixels)


@pytest.mark.parametrize('profile', PROFILES[3:], ids=lambda profile: profile[0])
def test_kodak_big_endian_coverage(profile):
    ranges, size, _ = _fixture(profile, '>')
    assert coverage.verified_camera_ranges('renamed.kdc', _exif(profile), ranges, size)


@pytest.mark.parametrize('profile_index,offset', [
    (0, 4), (0, 10), (0, 410), (0, 700), (0, 534), (0, 768), (0, 790),
    (0, 1112), (0, 1127), (0, 1512), (0, 1515), (0, 1520), (0, 1546),
    (3, 78), (3, 2048), (3, 2070), (3, 2205), (3, 2300), (3, 2359),
    (5, 512), (5, 522), (5, 768), (5, 1000), (5, 1105),
])
def test_single_missing_metadata_byte_rejects(profile_index, offset):
    profile = PROFILES[profile_index]
    ranges, size, _ = _fixture(profile)
    assert not coverage.verified_camera_ranges(profile[0], _exif(profile), _remove(ranges, offset), size)


@pytest.mark.parametrize('profile_index,offset,data', [
    (0, 4, struct.pack('<I', 0)), (0, 6, b'\xff\xff'),
    (0, 534, struct.pack('<I', 0)), (0, 534, struct.pack('<I', 257)),
    (0, 528, struct.pack('<H', 3)), (0, 512, struct.pack('<H', 4097)),
    (0, 768, struct.pack('<H', 4097)), (0, 790, struct.pack('<I', 0xffffffff)),
    (0, 818, struct.pack('<I', 256)),  # preview next-IFD points to itself
    (0, 814, struct.pack('<I', 0xffffffff)), (0, 1512, b'XX'),
    (0, 1516, b'\xff\xff'), (0, 1540, b'\xff\xd9'),
    (3, 78, struct.pack('<I', 0)), (3, 78, struct.pack('<I', 4096)),
    (3, 2048, struct.pack('<H', 4097)), (3, 2054, struct.pack('<I', 0xffffffff)),
    (3, 2052, struct.pack('<H', 3)), (3, 2134, struct.pack('<I', 2048)),
    (5, 522, struct.pack('<I', 0)), (5, 522, struct.pack('<I', 4096)),
    (5, 516, struct.pack('<H', 3)), (5, 768, b'\0\0'),
])
def test_corrupt_and_cyclic_references_reject(profile_index, offset, data):
    profile = PROFILES[profile_index]
    ranges, size, _ = _fixture(profile)
    assert not coverage.verified_camera_ranges(profile[0], _exif(profile), _replace(ranges, offset, data), size)


@pytest.mark.parametrize('changes', [
    {'Model': 'NX100'}, {'Make': 'Other'}, {'FileType': 'DNG'}, {},
])
def test_other_profiles_have_no_new_range_hurdle(changes):
    exif = {**_exif(PROFILES[0]), **changes} if changes else {}
    assert coverage.verified_camera_ranges('anything.srw', exif, None, -1)


@pytest.mark.parametrize('fault', ['empty', 'overlap', 'unsorted', 'too-many', 'size', 'boolean-offset', 'payload-budget'])
def test_malformed_ranges_and_budgets_reject(fault):
    ranges, size, _ = _fixture(PROFILES[0])
    if fault == 'empty':
        ranges = []
    elif fault == 'overlap':
        ranges.insert(1, (1, b'II'))
    elif fault == 'unsorted':
        ranges.reverse()
    elif fault == 'too-many':
        ranges = [(pos, b'\0') for pos in range(4097)]
    elif fault == 'size':
        size = True
    elif fault == 'boolean-offset':
        ranges[0] = (False, ranges[0][1])
    elif fault == 'payload-budget':
        size = 8 * 1024 * 1024 + 1
        ranges = [(0, bytes(size))]
    assert not coverage.verified_camera_ranges('photo.srw', _exif(PROFILES[0]), ranges, size)


def test_repeated_metadata_reads_have_a_cumulative_budget(monkeypatch):
    monkeypatch.setattr(coverage, '_MAX_BYTES', 1024)
    reader = coverage._Ranges([(0, bytes(1024))], 1024)
    for _ in range(8):
        reader.read(0, 1024)
    with pytest.raises(ValueError, match='budget'):
        reader.read(0, 1)


def _stored_packet(directory, name):
    import upload_metadata as transport
    index_path = PRIVATE / directory / 'index.json'
    if not index_path.exists():
        pytest.skip('Private local corpus packets are not present')
    index = json.loads(index_path.read_text())
    row = next(row for row in index['rows'] if row['name'] == name)
    assert row['status'] == 'generated'
    packet = (index_path.parent / (name + '.bin')).read_bytes()
    ranges = transport.decode_packet(packet, row['packet'], row['sourceBytes'])
    descriptor = {**row['packet'], 'sourceSize': row['sourceBytes'], 'physical': 'coverage-test', 'etag': 'local'}
    entity = {'uploadMetadataPacket': json.dumps(descriptor), 'fileHash': row['sourceSha256'],
              'anonymousImageId': 'coverage-test', 'size': row['sourceBytes']}
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'local'},
        _get_blob_client=lambda *_: SimpleNamespace(download_blob=lambda **_: SimpleNamespace(readall=lambda: packet)))
    return storage, entity, ranges, Path(index['root']) / name


@pytest.fixture
def real_exiftool():
    executable = shutil.which('exiftool')
    if not executable or subprocess.check_output([executable, '-ver'], text=True).strip() != '13.55':
        pytest.skip('Corpus integration requires the qualified ExifTool 13.55')


@pytest.mark.parametrize('profile', PROFILES, ids=lambda profile: profile[0])
def test_actual_fixed_packets_are_reused_with_exact_original_metadata(real_exiftool, profile):
    import exif_utils
    import upload_metadata as transport
    storage, entity, ranges, source = _stored_packet('exif-hardening-fixed-corpus', profile[0])
    assert coverage.verified_camera_ranges('renamed.bin', _exif(profile), ranges, entity['size'])
    outcomes = []
    actual = transport.extract_candidate(storage, profile[0], entity, on_outcome=outcomes.append)
    expected = exif_utils._extract_exiftool_from_path(str(source))
    for key in ('FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'):
        expected.pop(key, None)
    assert actual == expected
    assert outcomes == ['reused']


@pytest.mark.parametrize('profile', [PROFILES[index] for index in (0, 1, 2, 3, 4, 7)], ids=lambda profile: profile[0])
def test_actual_old_packets_reject_before_warning_qualification(real_exiftool, monkeypatch, profile):
    import exif_utils
    import upload_metadata as transport
    storage, entity, ranges, _ = _stored_packet('exif-hardening-corpus', profile[0])
    assert not coverage.verified_camera_ranges('renamed.bin', _exif(profile), ranges, entity['size'])

    def unexpected_warning_check(*_):
        pytest.fail('Incomplete metadata reached warning qualification')

    monkeypatch.setattr(exif_utils, '_exiftool_has_only_warnings', unexpected_warning_check)
    outcomes = []
    assert transport.extract_candidate(storage, profile[0], entity, on_outcome=outcomes.append) is None
    assert outcomes == ['incomplete_camera_metadata']
