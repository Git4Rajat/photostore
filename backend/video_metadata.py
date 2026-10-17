"""Validate browser video metadata; never decode or download a video original."""
import hashlib
import json
import math
import re

MAX_VIDEO_SOURCE_BYTES = 10 * 1024 * 1024 * 1024
MAX_VIDEO_METADATA_BYTES = 1024 * 1024
PARSER = 'mediainfo.js@0.3.8'
_ISO6709 = re.compile(r'^([+-]\d{2}(?:\.\d+)?)([+-]\d{3}(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?$')
_RECORDED_LOCATION = re.compile(
    r'^(\d+(?:\.\d+)?)\u00b0([NS])\s+(\d+(?:\.\d+)?)\u00b0([EW])'
    r'(?:\s+[+-]?\d+(?:\.\d+)?m)?$'
)


def decode_video_metadata(packet, descriptor, source_size):
    if (len(packet) > MAX_VIDEO_METADATA_BYTES or len(packet) != descriptor['bytes']
            or hashlib.sha256(packet).hexdigest() != descriptor['sha256']):
        raise ValueError('Video metadata checksum or size mismatch')
    value = json.loads(packet)
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
            or value.get('kind') != 'video-mediainfo-json' or value.get('parser') != PARSER
            or type(source_size) is not int or not 0 < source_size <= MAX_VIDEO_SOURCE_BYTES
            or type(value.get('sourceSize')) is not int or value['sourceSize'] != source_size
            or value.get('sourceSha256') != descriptor['sourceSha256']
            or ('readBudgetReached' in value and type(value['readBudgetReached']) is not bool)):
        raise ValueError('Invalid video metadata binding')
    tracks = value.get('tracks')
    if not isinstance(tracks, list) or not 0 < len(tracks) <= 64:
        raise ValueError('Invalid video metadata tracks')
    tags = {'MetadataParser': PARSER}
    if 'readBudgetReached' in value:
        tags['MetadataReadBudgetReached'] = str(value['readBudgetReached']).lower()
    scalars = []

    def flatten(item, prefix, depth=0):
        if depth > 12 or len(scalars) >= 4096:
            raise ValueError('Video metadata field limit')
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ValueError('Invalid video metadata key')
                flatten(child, prefix + '.' + key, depth + 1)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                flatten(child, prefix + '.' + str(index), depth + 1)
        elif item is not None:
            if not isinstance(item, (str, int, float, bool)) or (isinstance(item, float) and not math.isfinite(item)):
                raise ValueError('Invalid video metadata value')
            scalars.append((prefix, item))

    allowed_types = {'General', 'Video', 'Audio', 'Text', 'Other', 'Menu', 'Image'}
    video = None
    general = None
    for index, track in enumerate(tracks):
        if not isinstance(track, dict) or track.get('@type') not in allowed_types:
            raise ValueError('Invalid video metadata track')
        flatten(track, 'MediaInfo.' + track['@type'] + '.' + str(index))
        if track['@type'] == 'Video' and video is None:
            video = track
        if track['@type'] == 'General' and general is None:
            general = track
    if video is None or general is None or not general.get('Format'):
        raise ValueError('Unrecognized video metadata')
    from exif_utils import _stringify_exif_value, _trim_exif_for_storage
    tags['FileType'] = str(general['Format'])[:128]
    for key, field in [('Make', 'Encoded_Hardware_CompanyName'), ('Model', 'Encoded_Hardware_Name')]:
        if isinstance(general.get(field), str) and general[field]:
            tags[key] = _stringify_exif_value(general[field])
    for key, field in [('ImageWidth', 'Width'), ('ImageLength', 'Height')]:
        number = video.get(field)
        if type(number) in (int, float) and math.isfinite(number) and 0 < number <= 100000:
            tags[key] = str(int(number))
    for key, field in [('Duration', 'Duration'), ('VideoFrameRate', 'FrameRate'), ('Rotation', 'Rotation'), ('VideoCodec', 'Format')]:
        if video.get(field) is not None:
            tags[key] = _stringify_exif_value(video[field])
    # Preserve library-specific fields separately from the stable date/GPS keys.
    for field in ('Recorded_Date', 'Encoded_Date'):
        date = general.get(field) or video.get(field)
        if isinstance(date, str) and date.strip():
            tags['CreateDate'] = date
            break
    for key, item in scalars:
        if any(name in key.lower() for name in ('location', 'xyz', 'gps')) and isinstance(item, str):
            match = _ISO6709.fullmatch(item.strip())
            if match:
                lat, lon = float(match[1]), float(match[2])
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    tags['GPS.LatitudeDecimal'], tags['GPS.LongitudeDecimal'] = str(lat), str(lon)
                    break
    if 'GPS.LatitudeDecimal' not in tags:
        match = _RECORDED_LOCATION.fullmatch(str(general.get('Recorded_Location') or '').strip())
        if match:
            lat = float(match[1]) * (-1 if match[2] == 'S' else 1)
            lon = float(match[3]) * (-1 if match[4] == 'W' else 1)
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                tags['GPS.LatitudeDecimal'], tags['GPS.LongitudeDecimal'] = str(lat), str(lon)
    tags.update({key: _stringify_exif_value(item) for key, item in scalars})
    return _trim_exif_for_storage(tags)


def apply_browser_video_metadata(app, user_id, filename, client_asset_id):
    from image_utils import is_video_file
    if not is_video_file(filename):
        raise ValueError('Video metadata requires a video filename')
    from azure.core import MatchConditions
    import storage_utils
    from upload_metadata import packet_blob_name, validate_descriptor
    row = app.metadata_table_client.get_entity(partition_key=user_id, row_key=filename)
    candidate = json.loads(row.get('uploadMetadataPacket') or '{}')
    descriptor = validate_descriptor(candidate, row.get('fileHash'))
    source_size = candidate.get('sourceSize')
    if (candidate.get('physical') != row.get('anonymousImageId') or type(source_size) is not int
            or source_size != int(row.get('size') or 0) or not candidate.get('etag')
            or descriptor['bytes'] > MAX_VIDEO_METADATA_BYTES):
        raise ValueError('Invalid video metadata candidate')
    blob = storage_utils._get_blob_client(storage_utils._CTX['blob_thumbnail_container'], packet_blob_name(candidate['physical']))
    packet = blob.download_blob(offset=0, length=descriptor['bytes'], etag=candidate['etag'],
                                match_condition=MatchConditions.IfNotModified).readall()
    tags = decode_video_metadata(packet, descriptor, source_size)
    processing = {'exif': {'hasData': True, 'data': tags, 'source': 'browser_video_metadata', 'runtime': 'mediainfo_wasm'}}
    if 'GPS.LatitudeDecimal' in tags and 'GPS.LongitudeDecimal' in tags:
        processing['map_detection'] = {'hasData': True, 'latitude': tags['GPS.LatitudeDecimal'],
                                       'longitude': tags['GPS.LongitudeDecimal']}
    return app.apply_client_processing_results_for_file(user_id, filename, client_processing=processing,
        client_asset_id=client_asset_id, origin='browser_video_metadata', claimed_steps=['exif', 'map_detection'])
