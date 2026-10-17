"""Compare real original/packet metadata and diagnostics; never call the cloud.

Generate packets with frontend/scripts/exif-corpus-packets.mjs first. Reports
contain field names and aggregate counts, not EXIF/GPS values or parser text.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exif_utils
import upload_metadata

FILESYSTEM = {'FileModifyDate', 'FileAccessDate', 'FileInodeChangeDate', 'FilePermissions'}
IDENTITY = {'Make', 'Model', 'DateTimeOriginal', 'GPS.LatitudeDecimal', 'Orientation'}


def source_manifest(index):
    rows = index['rows']
    result = {r['name']: (r['sourceBytes'], r['sourceSha256']) for r in rows}
    if len(rows) != len(result):
        raise ValueError('Duplicate corpus entries')
    return result


def bind_target(index, report, baseline_index, baseline):
    """A fixed reference cohort prevents failures or subsets shrinking the target."""
    manifest = source_manifest(index)
    if manifest != source_manifest(baseline_index):
        raise ValueError('Target requires the complete unchanged baseline corpus')
    root = Path(index['root'])
    inventory = {p.name for p in root.iterdir() if p.is_file() and not p.name.startswith('.')}
    if set(manifest) != inventory:
        raise ValueError('Target requires the complete current corpus inventory')
    current = {r['name']: r for r in report['rows']}
    reference = {r['name']: r for r in baseline['rows']}
    if (len(current) != len(report['rows']) or len(reference) != len(baseline['rows'])
            or current.keys() != manifest.keys() or reference.keys() != manifest.keys()):
        raise ValueError('Incomplete or duplicate audit cohort')
    if report['exiftoolVersion'] != baseline['exiftoolVersion']:
        raise ValueError('Reference runtime differs; requalify the baseline')
    if any(r['sourceBytes'] != manifest[name][0] for name, r in current.items()):
        raise ValueError('Audit source size mismatch')
    eligible = {name for name, r in reference.items() if r['extractable']}
    exact = sum(bool(current[name].get('accepted') and current[name].get('acceptedExact')) for name in eligible)
    mismatches = sum(bool(r.get('accepted') and not r.get('acceptedExact')) for r in current.values())
    infrastructure = sum(r.get('originalFailure') not in {None, 'parser_error', 'unsupported'}
                         for r in current.values())
    regressions = sum(not current[name]['extractable'] for name in eligible)
    summary = report['summary']
    summary.update(referenceBound=True, referenceEligible=len(eligible),
        acceptedExact=exact, acceptedMismatch=mismatches,
        referenceExtractionRegressions=regressions, infrastructureFailures=infrastructure,
        exactReusePercent=round(100 * exact / len(eligible), 4) if eligible else None,
        target99Met=bool(eligible) and exact / len(eligible) >= .99
            and not mismatches and not infrastructure and not regressions)
    return summary


def verify_sources(index):
    for name, (size, digest) in source_manifest(index).items():
        source = Path(index['root']) / name
        if source.stat().st_size != size:
            raise ValueError('Corpus size changed')
        with source.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != digest:
                raise ValueError('Corpus bytes changed')


def extraction(path):
    try:
        with open(path, 'rb') as source:
            is_jpeg = source.read(2) == b'\xff\xd8'
        data = (exif_utils._extract_exiftool_from_path(str(path), scan_jpeg=True) if is_jpeg
                else exif_utils._extract_exiftool_from_path(str(path)))
        return {k: v for k, v in data.items() if k not in FILESYSTEM}, None
    except Exception as error:
        return {}, getattr(error, 'category', type(error).__name__)


def diagnostics(path):
    with open(path, 'rb') as source:
        jpeg = source.read(2) == b'\xff\xd8'
    result = subprocess.run(['exiftool', '-j', '-G4', *(['-ee'] if jpeg else []), '-Warning', '-Error', str(path)],
                            capture_output=True, timeout=30, check=False)
    parsed = json.loads(result.stdout or '[]')
    return sorted((key.rsplit(':', 1)[-1], str(value))
                  for key, value in parsed[0].items() if key != 'SourceFile')


def audit(directory):
    index = json.loads((directory / 'index.json').read_text())
    rows = []
    for item in index['rows']:
        name = item['name']
        source = Path(index['root']) / name
        if source.stat().st_size != item['sourceBytes']:
            raise ValueError('Corpus size changed')
        with source.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != item['sourceSha256']:
                raise ValueError('Corpus bytes changed')
        original, failure = extraction(source)
        original_diag = diagnostics(source)
        has_error = bool(failure or any(key == 'Error' for key, _ in original_diag))
        row = {'name': name, 'extension': source.suffix.lower(), 'sourceBytes': item['sourceBytes'],
               'generated': item['status'] == 'generated', 'originalFailure': failure,
               'originalError': has_error, 'originalWarningCount': len(original_diag),
               'extractable': bool(IDENTITY.intersection(original)) and not has_error,
               'originalTagCount': len(original), 'accepted': False}
        if not row['generated']:
            row['generationReason'] = item.get('reason')
        else:
            packet = (directory / (name + '.bin')).read_bytes()
            descriptor = item['packet']
            ranges = upload_metadata.decode_packet(packet, descriptor, item['sourceBytes'])
            with tempfile.TemporaryDirectory(prefix='photostore-exif-audit-') as temporary:
                rebuilt = Path(temporary) / name
                with rebuilt.open('wb') as handle:
                    handle.truncate(item['sourceBytes'])
                    for offset, data in ranges:
                        handle.seek(offset)
                        handle.write(data)
                sparse, sparse_failure = extraction(rebuilt)
                sparse_diag = diagnostics(rebuilt)
            row['packetFailure'] = sparse_failure
            row['diagnosticParity'] = original_diag == sparse_diag
            row['differingKeys'] = sorted(k for k in original.keys() | sparse.keys()
                                          if original.get(k) != sparse.get(k))
            row['exactParity'] = not row['differingKeys'] and not failure and not sparse_failure
            candidate = {**descriptor, 'physical': 'local-audit', 'sourceSize': item['sourceBytes'], 'etag': 'local'}
            entity = {'uploadMetadataPacket': json.dumps(candidate), 'anonymousImageId': 'local-audit',
                      'fileHash': item['sourceSha256'], 'size': item['sourceBytes']}
            storage = SimpleNamespace(_CTX={'blob_thumbnail_container': 'local'}, _get_blob_client=lambda *_:
                SimpleNamespace(download_blob=lambda **_: SimpleNamespace(readall=lambda: packet)))
            outcomes = []
            result = upload_metadata.extract_candidate(storage, name, entity, on_outcome=outcomes.append)
            row.update(accepted=result is not None, outcome=outcomes[-1] if outcomes else 'unknown')
            row['acceptedExact'] = (result is not None and not has_error
                and {k: v for k, v in result.items() if k not in FILESYSTEM} == original
                and row['diagnosticParity'])
        rows.append(row)
        if len(rows) % 50 == 0:
            print(json.dumps({'files': len(rows), 'acceptedExact': sum(r.get('acceptedExact', False) for r in rows)}), flush=True)
    eligible = [r for r in rows if r['extractable']]
    exact = sum(r.get('acceptedExact', False) for r in eligible)
    summary = {'files': len(rows), 'extractable': len(eligible), 'originalErrors': sum(r['originalError'] for r in rows),
        'generated': sum(r['generated'] for r in rows), 'accepted': sum(r['accepted'] for r in rows),
        'acceptedExact': exact, 'acceptedMismatch': sum(r['accepted'] and not r.get('acceptedExact', False) for r in rows),
        'exactReusePercent': round(100 * exact / len(eligible), 4) if eligible else None,
        'target99Met': None, 'referenceBound': False,
        'originalBytesAvoided': sum(r['sourceBytes'] for r in rows if r.get('acceptedExact')),
        'outcomes': dict(Counter(r.get('outcome') or r.get('generationReason') for r in rows))}
    report = {'cloudCalls': 0, 'exiftoolVersion': subprocess.check_output(['exiftool', '-ver'], text=True).strip(),
              'denominator': 'All originals with qualifying camera metadata and no ExifTool Error; warnings included',
              'summary': summary, 'families': {ext: {'files': len(group), 'extractable': sum(r['extractable'] for r in group),
                  'acceptedExact': sum(r.get('acceptedExact', False) for r in group)}
                  for ext in sorted({r['extension'] for r in rows})
                  for group in [[r for r in rows if r['extension'] == ext]]}, 'rows': rows}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packets', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline-index', type=Path)
    parser.add_argument('--baseline-report', type=Path)
    parser.add_argument('--recheck-report', type=Path,
                        help='Recheck source hashes and apply the fixed-cohort gate to an existing full extraction report')
    parser.add_argument('--require-target', action='store_true',
                        help='Fail unless exact reuse reaches 99%% with zero accepted mismatches')
    args = parser.parse_args()
    if bool(args.baseline_index) != bool(args.baseline_report):
        parser.error('Supply both baseline index and baseline report')
    if args.require_target and not args.baseline_index:
        parser.error('--require-target requires a fixed baseline index and report')
    index = json.loads((args.packets / 'index.json').read_text())
    if args.recheck_report:
        verify_sources(index)
        report = json.loads(args.recheck_report.read_text())
    else:
        report = audit(args.packets)
    if args.baseline_index:
        bind_target(index, report, json.loads(args.baseline_index.read_text()), json.loads(args.baseline_report.read_text()))
    args.output.write_text(json.dumps(report, indent=2))
    args.output.chmod(0o600)
    summary = report['summary']
    print(json.dumps(summary, indent=2))
    if args.require_target and (not summary['target99Met'] or summary['acceptedMismatch']):
        sys.exit(1)
