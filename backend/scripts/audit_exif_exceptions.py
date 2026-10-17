"""Classify the reference reader's exceptions without decoding sensor pixels."""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ipwork_geo


def probe_raw(path):
    # A subprocess bounds a native reader hang/crash; open_file does not unpack.
    code = '''
import json, rawpy, sys
try:
    with rawpy.RawPy() as reader:
        reader.open_file(sys.argv[1])
    print(json.dumps({'opened': True}))
except Exception as error:
    print(json.dumps({'opened': False, 'category': type(error).__name__}))
'''
    try:
        result = subprocess.run([sys.executable, '-c', code, str(path)],
                                capture_output=True, timeout=30, check=True)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {'opened': False, 'category': 'probe_timeout'}
    except (subprocess.CalledProcessError, ValueError):
        return {'opened': False, 'category': 'probe_failed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text())
    rows = []
    for item in baseline['rows']:
        if item['extractable']:
            continue
        path = args.corpus / item['name']
        if path.stat().st_size != item['sourceBytes']:
            raise ValueError('Corpus source changed')
        result = ipwork_geo.process_exif('local-audit', path.name, path.read_bytes())
        rows.append({'name': path.name, 'extension': path.suffix.lower(),
            'workerHasData': result['hasData'], 'workerError': result.get('errorCode'),
            'workerTagCount': len(result.get('data') or {}), 'libraw': probe_raw(path)})
    report = {'cloudCalls': 0, 'sensorUnpacks': 0, 'files': len(rows),
        'workerOutcomes': dict(Counter(r['workerError'] or ('metadata' if r['workerHasData'] else 'no_data') for r in rows)),
        'librawOutcomes': dict(Counter('opened' if r['libraw']['opened'] else r['libraw']['category'] for r in rows)),
        'rows': rows}
    args.output.write_text(json.dumps(report, indent=2))
    args.output.chmod(0o600)
    print(json.dumps({k: v for k, v in report.items() if k != 'rows'}, indent=2))
