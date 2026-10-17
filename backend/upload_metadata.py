"""Bounded metadata transport with format-validated JPEG absence qualification."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

import processing_diagnostics

MAX_PACKET_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
HASH = re.compile(r'^[a-f0-9]{64}$')
KINDS = {'tiff-ifd-ranges', 'ciff-directory-ranges', 'raf-header-jpeg-metadata-directories',
         'mrw-pre-sensor-header', 'x3f-directory-nonimage-sections', 'bmff-non-mdat-boxes', 'jpeg-header-ranges', 'png-chunk-ranges', 'webp-chunk-ranges'}
# Corpus-qualified families only. This is experimental, not universal camera coverage.
QUALIFIED_EXTENSIONS = {'.arw', '.cr2', '.cr3', '.dng', '.erf', '.gpr', '.mos', '.mrw',
                        '.nef', '.pef', '.raf', '.rw2', '.rwl', '.3fr', '.jpg', '.jpeg',
                        '.crw', '.fff', '.iiq', '.nrw', '.orf', '.ori', '.raw', '.srw',
                        '.tif', '.tiff', '.sti', '.x3f', '.mef', '.heic', '.heif', '.hif', '.png', '.webp', '.dcr', '.kdc'}


def _verified_source_warning_profile(filename, exif):
    """Exact source diagnostics qualified by original/packet corpus parity.

    These warnings remain in the saved metadata; no -m or -ignoreMinorErrors.
    The caller must independently check *all* diagnostics, including duplicates.
    """
    if (exif.get('ExifToolVersion') != '13.55'
            or any(key in exif for key in ('Error', 'EXIF.Truncated'))):
        return ()
    ext = Path(filename).suffix.lower()
    make, model, kind = (exif.get(key) for key in ('Make', 'Model', 'FileType'))
    warning = None
    if (ext == '.srw' and make == 'SAMSUNG' and kind == 'SRW'
            and model in {'EX1', 'EX2F', 'NX mini', 'NX1000', 'NX100', 'NX10', 'NX1100',
                          'NX11', 'NX200', 'NX20', 'NX210', 'NX3000', 'NX3300', 'NX5', 'WB2000'}):
        warning = '[minor] Overlapping MakerNotes values'
    elif ext == '.x3f' and make == 'SIGMA' and kind == 'X3F':
        if model in {'SIGMA DP1 Merrill', 'SIGMA DP2 Merrill', 'SIGMA DP3 Merrill'}:
            warning = 'Bad PrintIM size'
        elif model == 'SIGMA SD15':
            warning = 'Invalid EXIF text encoding for UserComment'
    elif (ext == '.dng' and kind == 'DNG' and make == 'Arashi Vision'
            and model == 'Insta360 One2' and exif.get('DNGVersion') == '1 1 0 0'):
        warning = 'Invalid EXIF text encoding for UserComment'
    elif ext == '.fff' and kind == 'FFF' and make == 'Hasselblad':
        if model in {'Hasselblad CFV-50c', 'Hasselblad X2D 100C'}:
            warning = 'Bad IPTC data tag (marker 0x0) [x2]'
        elif model in {'Hasselblad H5D-40', 'Hasselblad H5D-50c'}:
            warning = '[minor] Error reading value for IFD0 entry 26, ID 0xc519 HasselbladXML'
        elif model == 'Hasselblad H6D-100cMS':
            warning = '[minor] Error reading value for IFD0 entry 27, ID 0xc519 HasselbladXML'
        elif (model == 'Hasselblad H4D-40'
                and exif.get('Warning') == '[minor] Suspicious MakerNotes offset for tag 0x001a'):
            return ('[minor] Suspicious MakerNotes offset for tag 0x001a',
                    '[minor] Error reading value for IFD0 entry 27, ID 0xc519 HasselbladXML')
    elif ext == '.iiq' and kind == 'IIQ':
        if make == 'Phase One A/S' and model == 'H 25':
            warning = 'ICC_Profile pointer references previous ICC_Profile directory'
        elif make == 'Phase One' and model == 'iXM-RS150F':
            warning = '[minor] Unrecognized PhaseOne format size 8 [x3]'
    elif ext in {'.tif', '.tiff'} and kind == 'TIFF' and (make, model) in {
            ('Canon', 'EOS D2000C'), ('Kodak', 'DCS520C')}:
        warning = '[minor] Empty IPTC-NAA data'
    elif ext == '.dcr' and kind == 'DCR' and make == 'Kodak':
        if model == 'DCS Pro 14N' and exif.get('Warning') == '[Minor] Ignoring KodakIFD DiffTileGains24t852822 with excessive count':
            return ('[Minor] Ignoring KodakIFD DiffTileGains24t852822 with excessive count',
                    '[minor] Empty IPTC-NAA data')
        if model in {'DCS Pro 14nx', 'DCS Pro SLR/c', 'DCS Pro SLR/n', 'DCS760C'}:
            warning = '[minor] Empty IPTC-NAA data'
    elif ext == '.kdc' and kind == 'KDC' and make == 'EASTMAN KODAK COMPANY':
        warning = 'Suspicious SubIFD offset for tag 0xfd04'
        if model in {'KODAK P712 ZOOM DIGITAL CAMERA', 'KODAK P880 ZOOM DIGITAL CAMERA'}:
            return (warning, 'Bad format (20033) for InteropIFD entry 0') if exif.get('Warning') == warning else ()
        if model not in {'KODAK EasyShare Z981 Digital Camera', 'KODAK EasyShare Z990 Digital Camera',
                         'KODAK EASYSHARE Z1015 IS DIGITAL CAMERA'}:
            warning = None
    return (warning,) if warning and exif.get('Warning') == warning else ()


def _verified_dng_array_warning(filename, exif):
    # Default ExifTool intentionally summarizes these arrays; do not enable -m
    # or suppress corruption warnings. Only corpus-verified profiles qualify.
    if (Path(filename).suffix.lower() != '.dng'
            or any(key in exif for key in ('Error', 'EXIF.Truncated'))
            or exif.get('ExifToolVersion') != '13.55' or exif.get('FileType') != 'DNG'):
        return False
    warning = '[Minor] Not decoding some large array(s). Ignore minor errors to decode'
    profiles = {
        ('Apple', 'iPhone 12 Pro', '1 4 0 0'): {'Warning': warning},
        ('Apple', 'iPhone 12 Pro Max', '1 6 0 0'): {'Warning': warning},
        ('Canon', 'Canon EOS 5D Mark III', '1 4 0 0'): {
            'Warning': warning + ' [x3]',
            'ProfileHueSatMapData1': '(large array of 8100 float values)',
            'ProfileHueSatMapData2': '(large array of 8100 float values)',
            'ProfileLookTableData': '(large array of 13824 float values)',
        },
        ('Autel Robotics', 'XB015', '1 4 0 0'): {
            'Warning': warning + ' [x2]',
            'ProfileHueSatMapData1': '(large array of 2250 float values)',
            'ProfileHueSatMapData2': '(large array of 2250 float values)',
        },
    }
    expected = profiles.get((exif.get('Make'), exif.get('Model'), exif.get('DNGVersion')))
    return bool(expected) and all(exif.get(key) == value for key, value in expected.items())


def _qualified_png_metadata(filename, exif):
    # PNG screenshots often have ICC/text metadata but no camera identity tags.
    # Require parsed PNG header fields, never accept file/system tags alone.
    return (Path(filename).suffix.lower() == '.png' and exif.get('FileType') == 'PNG'
            and all(re.fullmatch(r'[1-9]\d{0,9}', str(exif.get(key) or ''))
                    and int(exif[key]) <= 0x7fffffff
                    for key in ('ImageWidth', 'ImageHeight'))
            and exif.get('BitDepth') in {'1', '2', '4', '8', '16'}
            and exif.get('ColorType') in {'0', '2', '3', '4', '6'}
            and exif.get('Compression') == '0' and exif.get('Filter') == '0'
            and exif.get('Interlace') in {'0', '1'})


def _verified_seft_trailer(ranges, source_size, jpeg_end):
    from metadata_tiff_coverage import _Ranges
    try:
        reader = _Ranges(ranges, source_size)
        footer = reader.read(source_size - 8, 8)
        length = int.from_bytes(footer[:4], 'little')
        if footer[4:] != b'SEFT' or not 12 <= length < 65536 or length + 8 >= source_size:
            return False
        start = source_size - length - 8
        directory = reader.read(start, length + 8)
        count = int.from_bytes(directory[8:12], 'little')
        if directory[:4] != b'SEFH' or not 0 < count <= 4096 or 12 + count * 12 > length:
            return False
        first = start
        for index in range(count):
            entry = directory[12 + index * 12:24 + index * 12]
            backwards, size = int.from_bytes(entry[4:8], 'little'), int.from_bytes(entry[8:12], 'little')
            if not 8 <= size <= backwards <= start:
                return False
            first = min(first, start - backwards)
            block = reader.read(start - backwards, size)
            if int.from_bytes(block[4:8], 'little') + 8 > size:
                return False
        # Reject gaps and unknown trailing payload, not just a matching footer.
        reader.read(jpeg_end, source_size - jpeg_end)
        return first == jpeg_end
    except (ValueError, TypeError, IndexError):
        return False


def _mpf_extents(data, tiff_base, source_size):
    """Bounded MP Index directory; all constituent images must be JPEG."""
    if data[:4] != b'MPF\0' or len(data) < 12:
        raise ValueError('Invalid MPF header')
    block = data[4:]
    order = {b'II': 'little', b'MM': 'big'}.get(block[:2])
    def number(offset, length):
        if offset < 0 or offset + length > len(block):
            raise ValueError('MPF directory bounds')
        return int.from_bytes(block[offset:offset + length], order)
    if not order or number(2, 2) != 42:
        raise ValueError('Invalid MPF TIFF header')
    directory = number(4, 4)
    count = number(directory, 2)
    if not 1 <= count <= 128 or number(directory + 2 + count * 12, 4):
        raise ValueError('Unsupported MPF directory')
    tags = {}
    for index in range(count):
        pos = directory + 2 + index * 12
        tag, kind, length, value = (number(pos, 2), number(pos + 2, 2), number(pos + 4, 4), number(pos + 8, 4))
        if tag in tags:
            raise ValueError('Duplicate MPF entry')
        tags[tag] = (kind, length, value)
    if 0xb001 not in tags and 0xb002 not in tags:
        return None  # Attribute-only MPF directory, including a child JPEG.
    kind, length, images = tags[0xb001]
    if kind != 4 or length != 1 or not 2 <= images <= 32:
        raise ValueError('Unsupported MPF image count')
    kind, length, offset = tags[0xb002]
    if kind != 7 or length != images * 16:
        raise ValueError('Invalid MPF image table')
    extents = []
    for index in range(images):
        pos = offset + index * 16
        attributes, size, relative = number(pos, 4), number(pos + 4, 4), number(pos + 8, 4)
        start = 0 if index == 0 and relative == 0 else tiff_base + relative
        if attributes & 0x07000000 or size < 4 or start < 0 or start + size > source_size:
            raise ValueError('Unsupported MPF image extent')
        extents.append((start, start + size))
    if (extents[0][0] != 0 or extents[-1][1] != source_size
            or any(left[1] != right[0] for left, right in zip(extents, extents[1:]))):
        raise ValueError('Incomplete or overlapping MPF image layout')
    return extents


def _verified_jpeg_structure(filename, ranges, source_size, exif, *, metadata=False, check_dimensions=True, _extent=None):
    """Validate every marker through EOI; metadata segments must be retained."""
    if Path(filename).suffix.lower() not in {'.jpg', '.jpeg'} or exif.get('FileType') != 'JPEG':
        return False
    index = 0
    def read(offset, length):
        nonlocal index
        while index < len(ranges) and offset >= ranges[index][0] + len(ranges[index][1]):
            index += 1
        if index >= len(ranges):
            raise ValueError('Incomplete JPEG structure')
        start, data = ranges[index]
        relative = offset - start
        if relative < 0 or relative + length > len(data):
            raise ValueError('Missing JPEG marker bytes')
        return data[relative:relative + length]
    try:
        start, end = _extent or (0, source_size)
        if read(start, 2) != b'\xff\xd8':
            return False
        pos, frame, scans, mpf = start + 2, None, 0, None
        for _ in range(1024):
            marker = read(pos, 2)
            if marker == b'\xff\xd9':
                if mpf:
                    if mpf[0] != (0, pos + 2):
                        return False
                    return bool(frame and scans and all(_verified_jpeg_structure(
                        filename, ranges, source_size, {'FileType': 'JPEG'}, metadata=True,
                        check_dimensions=False, _extent=extent) for extent in mpf[1:]))
                if _extent:
                    return bool(frame and scans and pos + 2 == end)
                complete_end = (pos + 2 == end and index == len(ranges) - 1
                                and ranges[index][0] + len(ranges[index][1]) == source_size)
                return bool(frame and scans and (complete_end or (metadata
                    and _verified_seft_trailer(ranges, source_size, pos + 2))))
            application = metadata and (0xe0 <= marker[1] <= 0xef or marker[1] == 0xfe)
            if marker[0] != 255 or (not application and marker[1] not in {0xc0, 0xc2, 0xc4, 0xdb, 0xdd, 0xda, 0xe0}):
                return False
            size = int.from_bytes(read(pos + 2, 2), 'big')
            if size < 2 or pos + size + 2 > end:
                return False
            data = read(pos + 4, size - 2)
            if marker[1] == 0xe2 and data.startswith(b'MPF\0'):
                if not metadata:
                    return False
                image_index = _mpf_extents(data, pos + 8, source_size)
                if image_index:
                    if mpf or _extent:
                        return False
                    mpf = image_index
            if marker[1] == 0xe0 and not metadata:
                if len(data) < 14 or not data.startswith(b'JFIF\0') or len(data) != 14 + 3 * data[12] * data[13]:
                    return False
            # Camera/vendor application segments are opaque but fully retained.
            # Only the stricter absence path requires a known marker vocabulary.
            if marker[1] in {0xc0, 0xc2}:
                if frame or len(data) < 6 or data[0] != 8 or data[5] not in ({1, 3, 4} if metadata else {1, 3}) or len(data) != 6 + 3 * data[5]:
                    return False
                height, width = int.from_bytes(data[1:3], 'big'), int.from_bytes(data[3:5], 'big')
                if not width or not height or (check_dimensions and (
                        str(width) != exif.get('ImageWidth') or str(height) != exif.get('ImageHeight'))):
                    return False
                frame = data[5]
            if marker[1] == 0xdd and len(data) != 2:
                return False
            pos += size + 2
            if marker[1] == 0xda:
                if not frame or not data or not 1 <= data[0] <= frame or len(data) != 4 + 2 * data[0]:
                    return False
                scans += 1
                # A complete packet ends each SOS range exactly at the entropy
                # boundary and retains the next marker at its original offset.
                if pos != ranges[index][0] + len(ranges[index][1]) or index + 1 >= len(ranges):
                    return False
                index += 1
                pos = ranges[index][0]
        return False
    except (ValueError, IndexError, TypeError, KeyError):
        return False


def _verified_jpeg_no_exif(filename, ranges, source_size, exif):
    return _verified_jpeg_structure(filename, ranges, source_size, exif)


def _qualified_webp_metadata(filename, ranges, source_size, exif):
    if (Path(filename).suffix.lower() != '.webp'
            or exif.get('FileType') not in {'WEBP', 'WEBP (lossless)', 'Extended WEBP', 'Extended WEBP (lossless)'}):
        return False
    index = 0
    def read(offset, length):
        nonlocal index
        while index < len(ranges) and offset >= ranges[index][0] + len(ranges[index][1]):
            index += 1
        if index >= len(ranges):
            raise ValueError('Incomplete WebP packet')
        start, data = ranges[index]
        relative = offset - start
        if relative < 0 or relative + length > len(data):
            raise ValueError('Missing WebP bytes')
        return data[relative:relative + length]
    try:
        header = read(0, 12)
        if header[:4] != b'RIFF' or header[8:] != b'WEBP' or int.from_bytes(header[4:8], 'little') + 8 != source_size:
            return False
        pos, dimensions, extended, flags, seen = 12, None, None, 0, set()
        for _ in range(4096):
            header = read(pos, 8)
            kind, length = header[:4], int.from_bytes(header[4:], 'little')
            end = pos + 8 + length
            if end + (length & 1) > source_size or kind in seen:
                return False
            seen.add(kind)
            if kind == b'VP8X':
                if pos != 12 or length != 10:
                    return False
                data = read(pos + 8, 10)
                flags = data[0]
                if flags & 0xc3 or any(data[1:4]):
                    return False
                extended = (1 + int.from_bytes(data[4:7], 'little'), 1 + int.from_bytes(data[7:10], 'little'))
            elif kind in {b'VP8 ', b'VP8L'}:
                prefix = 10 if kind == b'VP8 ' else 5
                if dimensions or length < prefix:
                    return False
                data = read(pos + 8, prefix)
                if kind == b'VP8 ':
                    if data[0] & 1 or data[3:6] != b'\x9d\x01\x2a':
                        return False
                    dimensions = (int.from_bytes(data[6:8], 'little') & 0x3fff, int.from_bytes(data[8:10], 'little') & 0x3fff)
                else:
                    bits = int.from_bytes(data[1:], 'little')
                    if data[0] != 0x2f or bits >> 29:
                        return False
                    dimensions = (1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
            elif kind in {b'ICCP', b'EXIF', b'XMP '}:
                if extended is None or not length:
                    return False
                read(pos + 8, length)
            elif kind == b'ALPH':
                if extended is None or dimensions or not length:
                    return False
                if read(pos + 8, 1)[0] & 0xc0:
                    return False
            else:
                return False
            if length & 1 and read(end, 1) != b'\x00':
                return False
            pos = end + (length & 1)
            if pos == source_size:
                return bool(dimensions and all(dimensions) and (extended is None or extended == dimensions)
                    and tuple(map(str, dimensions)) == (exif.get('ImageWidth'), exif.get('ImageHeight'))
                    and all(bool(flags & flag) == (kind in seen) for flag, kind in ((0x20, b'ICCP'), (0x08, b'EXIF'), (0x04, b'XMP ')))
                    and index == len(ranges) - 1)
        return False
    except (ValueError, IndexError, TypeError):
        return False


def uploads_enabled(filename=''):
    from image_utils import is_video_file
    return is_video_file(filename) or os.getenv('BROWSER_UPLOAD_PREPARATION', 'true').lower() == 'true'


def qualified_family(filename):
    from image_utils import is_video_file
    return is_video_file(filename) or Path(filename).suffix.lower() in QUALIFIED_EXTENSIONS


def packet_blob_name(physical):
    return f'metadata/{physical}.bin'


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid metadata integer')
    return value


def validate_descriptor(descriptor, source_hash):
    if not isinstance(descriptor, dict) or type(descriptor.get('version')) is not int or descriptor['version'] != 1:
        raise ValueError('Invalid metadata version')
    size = _integer(descriptor.get('bytes'), 5, MAX_PACKET_BYTES)
    digest = descriptor.get('sha256')
    if not isinstance(digest, str) or not HASH.fullmatch(digest):
        raise ValueError('Invalid metadata digest')
    if not isinstance(source_hash, str) or not HASH.fullmatch(source_hash) or descriptor.get('sourceSha256') != source_hash:
        raise ValueError('Metadata source mismatch')
    return {'version': 1, 'bytes': size, 'sha256': digest, 'sourceSha256': source_hash}


def stamp_candidate(app, user_id, filename, descriptor, physical, total_size, source_hash):
    """Persist HEAD-bound candidate before queueing; parse only in the worker."""
    if not uploads_enabled(filename) or not descriptor:
        return
    import storage_utils
    clean = validate_descriptor(descriptor, source_hash)
    row = app.metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    if row.get('anonymousImageId') != physical or row.get('fileHash') != source_hash or int(row.get('size') or 0) != total_size:
        raise ValueError('Metadata reservation mismatch')
    blob = storage_utils._get_blob_client(storage_utils._CTX['blob_thumbnail_container'], packet_blob_name(physical))
    props = blob.get_blob_properties()
    if props.size != clean['bytes'] or props.content_settings.content_type != 'application/octet-stream':
        raise ValueError('Metadata blob mismatch')
    clean.update({'physical': physical, 'sourceSize': total_size, 'etag': str(props.etag)})
    app._update_metadata_entity_fields(user_id, filename, {'uploadMetadataPacket': json.dumps(clean, separators=(',', ':'))})


def decode_packet(packet, descriptor, source_size):
    if len(packet) != descriptor['bytes'] or hashlib.sha256(packet).hexdigest() != descriptor['sha256']:
        raise ValueError('Metadata checksum mismatch')
    length = int.from_bytes(packet[:4], 'big')
    if not 0 < length <= MAX_MANIFEST_BYTES or 4 + length > len(packet):
        raise ValueError('Invalid metadata header')
    manifest = json.loads(packet[4:4 + length])
    if not isinstance(manifest, dict) or type(manifest.get('version')) is not int or manifest['version'] != 1 or manifest.get('kind') not in KINDS:
        raise ValueError('Invalid metadata manifest')
    _integer(source_size, 1, MAX_SOURCE_BYTES)
    if type(manifest.get('sourceSize')) is not int or manifest['sourceSize'] != source_size or manifest.get('sourceSha256') != descriptor['sourceSha256']:
        raise ValueError('Invalid metadata source')
    ranges = manifest.get('ranges')
    if not isinstance(ranges, list) or not 0 < len(ranges) <= 4096:
        raise ValueError('Invalid metadata ranges')
    end = 0
    payload = 4 + length
    decoded = []
    for item in ranges:
        if not isinstance(item, dict):
            raise ValueError('Invalid metadata range')
        offset = _integer(item.get('offset'), 0, source_size - 1)
        count = _integer(item.get('length'), 1, MAX_PACKET_BYTES)
        if offset < end or offset + count > source_size or payload + count > len(packet):
            raise ValueError('Overlapping or out-of-bounds metadata range')
        decoded.append((offset, packet[payload:payload + count]))
        end, payload = offset + count, payload + count
    if decoded[0][0] != 0 or payload != len(packet):
        raise ValueError('Incomplete metadata envelope')
    return decoded


def _extraction_diagnostic(exif):
    version = str(exif.get('ExifToolVersion') or '')
    if not re.fullmatch(r'\d{1,3}\.\d{1,3}', version):
        version = 'unknown'
    warning = str(exif.get('Warning') or '')
    category = 'none'
    if 'Error' in exif:
        category = 'parser_error'
    elif 'EXIF.Truncated' in exif:
        category = 'storage_limit'
    elif 'Warning' in exif:
        category = 'unclassified_warning'
        for text, name in (
            ('original decision data', 'canon_decision_data'),
            ('VignettingCorr', 'canon_vignetting'),
            ('Not decoding some large array', 'large_array_limit'),
            ('Overlapping MakerNotes', 'overlapping_maker_notes'),
            ('IPTC', 'iptc'),
            ('PrintIM', 'print_im'),
            ('UserComment', 'user_comment_encoding'),
            ('SensorCalibration', 'sensor_calibration'),
        ):
            if text in warning:
                category = name
                break
    return {'version': version, 'category': category}


def extract_candidate(storage, filename, row, on_download=None, on_outcome=None, on_diagnostic=None):
    """Return validated metadata or None for fallback; qualify absence structurally."""
    from azure.core import MatchConditions
    from exif_utils import (ExifExtractionError, _extract_exiftool_from_path, _exiftool_has_only_warning,
                            _exiftool_has_only_warnings)
    def outcome(reason):
        if on_outcome:
            try:
                on_outcome(reason)
            except Exception:
                pass
    if not qualified_family(filename):
        outcome('unqualified_family')
        return None
    phase = 'binding_validation'
    try:
        candidate = json.loads(row.get('uploadMetadataPacket') or '{}')
        descriptor = validate_descriptor(candidate, row.get('fileHash'))
        source_size = _integer(candidate.get('sourceSize'), 1, MAX_SOURCE_BYTES)
        if candidate.get('physical') != row.get('anonymousImageId') or source_size != int(row.get('size') or 0) or not candidate.get('etag'):
            raise ValueError('Stale metadata binding')
        phase = 'packet_download'
        blob = storage._get_blob_client(storage._CTX['blob_thumbnail_container'], packet_blob_name(candidate['physical']))
        started, downloaded, download_outcome = time.monotonic(), 0, 'error'
        try:
            packet = blob.download_blob(offset=0, length=descriptor['bytes'], etag=candidate['etag'],
                                        match_condition=MatchConditions.IfNotModified).readall()
            downloaded, download_outcome = len(packet), 'success'
        finally:
            with processing_diagnostics.media_download_scope('metadata_packet'):
                processing_diagnostics.media_download('metadata_packet', downloaded,
                    (time.monotonic() - started) * 1000, download_outcome)
        if on_download:
            on_download(len(packet))
        phase = 'packet_validation'
        ranges = decode_packet(packet, descriptor, source_size)
        phase = 'exiftool_extraction'
        with tempfile.TemporaryDirectory(prefix='photostore-metadata-') as directory:
            path = os.path.join(directory, 'source' + Path(filename).suffix.lower())
            # Sparse holes preserve the original offsets without copying sensor pixels.
            with open(path, 'wb') as handle:
                handle.truncate(source_size)
                for offset, data in ranges:
                    handle.seek(offset)
                    handle.write(data)
            prefix = ranges[0][1][:2]
            if len(prefix) == 1 and len(ranges) > 1 and ranges[1][0] == 1:
                prefix += ranges[1][1][:1]
            packet_jpeg = prefix == b'\xff\xd8'
            if packet_jpeg:
                if not _verified_jpeg_structure(filename, ranges, source_size,
                        {'FileType': 'JPEG'}, metadata=True, check_dimensions=False):
                    outcome('incomplete_jpeg_metadata')
                    return None
                exif = _extract_exiftool_from_path(path, scan_jpeg=True)
            else:
                exif = _extract_exiftool_from_path(path)
            if packet_jpeg:
                if not _verified_jpeg_structure(filename, ranges, source_size, exif, metadata=True):
                    outcome('incomplete_jpeg_metadata')
                    return None
            from metadata_tiff_coverage import verified_camera_ranges
            if not verified_camera_ranges(filename, exif, ranges, source_size):
                outcome('incomplete_camera_metadata')
                return None
            verified_warning = (bool(exif) and _verified_dng_array_warning(filename, exif)
                                and _exiftool_has_only_warning(path, exif['Warning']))
            expected_warnings = _verified_source_warning_profile(filename, exif or {})
            if expected_warnings and not verified_warning:
                verified_warning = _exiftool_has_only_warnings(path, expected_warnings)
        if on_diagnostic:
            try:
                on_diagnostic(_extraction_diagnostic(exif or {}))
            except Exception:
                pass
        if (not exif or any(key in exif for key in ('Error', 'EXIF.Truncated'))
                or ('Warning' in exif and not verified_warning)):
            outcome('exiftool_warning' if exif else 'no_exif')
            return None
        webp_metadata = _qualified_webp_metadata(filename, ranges, source_size, exif)
        if Path(filename).suffix.lower() == '.webp' and not webp_metadata:
            outcome('unqualified_exif')
            return None
        # File/system tags alone do not establish complete or absent EXIF.
        has_identity = any(key in exif for key in ('Make', 'Model', 'DateTimeOriginal', 'GPS.LatitudeDecimal', 'Orientation'))
        no_exif = not has_identity and _verified_jpeg_no_exif(filename, ranges, source_size, exif)
        if (not has_identity
                and not _qualified_png_metadata(filename, exif) and not no_exif
                and not _verified_jpeg_structure(filename, ranges, source_size, exif, metadata=True)
                and not webp_metadata):
            outcome('unqualified_exif')
            return None
        for key in ('FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'):
            exif.pop(key, None)
        outcome('reused_no_exif' if no_exif else 'reused')
        return exif
    except ExifExtractionError as error:
        if on_diagnostic:
            try:
                on_diagnostic({'version': 'unknown', 'category': error.category})
            except Exception:
                pass
        outcome('exiftool_' + error.category)
        return None
    except Exception:
        outcome(phase + '_failed')
        return None
