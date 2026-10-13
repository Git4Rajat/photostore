"""Verify local browser packets with real ExifTool, without cloud calls."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exif_utils import _extract_exiftool_from_path
from upload_metadata import extract_candidate, QUALIFIED_EXTENSIONS

directory = Path(sys.argv[1])
corpus = Path(sys.argv[2])
results = json.loads((directory / 'results.json').read_text())
rows = []
for item in results['rows']:
    name = item['name']
    descriptor = item.get('metadata') or {}
    if descriptor.get('status') != 'generated':
        rows.append({'name': name, 'status': 'browser_fallback'})
        continue
    source = corpus / ('RAW_unique_by_format' if name.startswith('sample_') else 'hq50k_images') / name
    packet = (directory / (name + '-metadata.bin')).read_bytes()
    candidate = {**descriptor, 'physical': 'physical', 'sourceSize': source.stat().st_size, 'etag': 'local'}
    row = {'uploadMetadataPacket': json.dumps(candidate), 'anonymousImageId': 'physical',
           'fileHash': descriptor['sourceSha256'], 'size': source.stat().st_size}
    storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'local'},
        _get_blob_client=lambda *_: SimpleNamespace(download_blob=lambda **_: SimpleNamespace(readall=lambda: packet)))
    exif = extract_candidate(storage, name, row)
    if exif is None:
        rows.append({'name': name, 'status': 'backend_fallback', 'qualified': source.suffix.lower() in QUALIFIED_EXTENSIONS})
        continue
    original = _extract_exiftool_from_path(str(source))
    # Filesystem timestamps/permissions describe the temporary reconstruction.
    excluded = {'FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'}
    expected = {key: value for key, value in original.items() if key not in excluded}
    differences = sorted(key for key in set(expected) | set(exif) if expected.get(key) != exif.get(key))
    rows.append({'name': name, 'status': 'reused', 'tagCount': len(exif), 'differences': differences})
    if differences:
        raise AssertionError(f'{name}: metadata mismatch: {differences}')
print(json.dumps({'status': 'passed', 'rows': rows}, indent=2))
