"""Bounded browser metadata transport. Candidates never imply confirmed absence."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

MAX_PACKET_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
HASH = re.compile(r'^[a-f0-9]{64}$')
KINDS = {'tiff-ifd-ranges', 'ciff-directory-ranges', 'raf-header-jpeg-metadata-directories',
         'mrw-pre-sensor-header', 'x3f-directory-nonimage-sections', 'bmff-non-mdat-boxes', 'jpeg-header-ranges'}
# Corpus-qualified families only. This is experimental, not universal camera coverage.
QUALIFIED_EXTENSIONS = {'.arw', '.cr2', '.cr3', '.dng', '.erf', '.gpr', '.mos', '.mrw',
                        '.nef', '.pef', '.raf', '.rw2', '.rwl', '.3fr', '.jpg', '.jpeg',
                        '.crw', '.fff', '.iiq', '.nrw', '.orf', '.ori', '.raw', '.srw',
                        '.tif', '.tiff', '.sti', '.x3f', '.mef', '.heic', '.heif', '.hif'}


def uploads_enabled():
    return os.getenv('BROWSER_UPLOAD_PREPARATION', 'false').lower() == 'true'


def qualified_family(filename):
    return Path(filename).suffix.lower() in QUALIFIED_EXTENSIONS


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
    if not uploads_enabled() or not descriptor:
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
    """Return EXIF or None for original fallback. Never trust packet-only no_data."""
    from azure.core import MatchConditions
    from exif_utils import _extract_exiftool_from_path
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
        packet = blob.download_blob(offset=0, length=descriptor['bytes'], etag=candidate['etag'],
                                    match_condition=MatchConditions.IfNotModified).readall()
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
            exif = _extract_exiftool_from_path(path)
        if on_diagnostic:
            try:
                on_diagnostic(_extraction_diagnostic(exif or {}))
            except Exception:
                pass
        if not exif or any(key in exif for key in ('Warning', 'Error', 'EXIF.Truncated')):
            outcome('exiftool_warning' if exif else 'no_exif')
            return None
        # File/system tags alone do not establish complete or absent EXIF.
        if not any(key in exif for key in ('Make', 'Model', 'DateTimeOriginal', 'GPS.LatitudeDecimal', 'Orientation')):
            outcome('unqualified_exif')
            return None
        for key in ('FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'):
            exif.pop(key, None)
        outcome('reused')
        return exif
    except Exception:
        outcome(phase + '_failed')
        return None
