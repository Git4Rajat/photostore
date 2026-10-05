"""Read-only audit of latest uploads; uses existing Entra/Azure CLI login.

Tables have no ORDER BY: exhaust a projected partition scan, retain only a
bounded latest-upload heap, then retrieve diagnostics for that upload interval.
No photo/face/queue writes, account keys, or credentials are requested.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import heapq
import json
import time

from azure.data.tables import TableServiceClient
from azure.identity import AzureCliCredential


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', required=True)
    parser.add_argument('--library', required=True)
    parser.add_argument('--table', default='photometadata')
    parser.add_argument('--latest', type=int, default=10000)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if not 1 <= args.latest <= 10000:
        parser.error('--latest must be between 1 and 10000')
    started = time.monotonic()
    client = TableServiceClient(f'https://{args.account}.table.core.windows.net',
                               credential=AzureCliCredential(), connection_timeout=5,
                               read_timeout=20, retry_total=0).get_table_client(args.table)
    base = "PartitionKey eq '" + args.library.replace("'", "''") + "'"
    selected = []
    statuses = Counter()
    scanned = missing_dates = deleted = 0

    def check_budget():
        if time.monotonic() - started > 900:
            raise TimeoutError('Audit exceeded 15-minute budget; no partial counts reported')

    for page in client.query_entities(base, select=['RowKey', 'uploadDate', 'face_status',
                                                   'faceCount', 'deleted'], timeout=15).by_page():
        check_budget()
        for row in page:
            scanned += 1
            if scanned > 1000000:
                raise ValueError('Audit row bound exceeded')
            if row.get('deleted'):
                deleted += 1
                continue
            statuses[str(row.get('face_status') or 'missing')] += 1
            date = timestamp(row.get('uploadDate'))
            if date is None:
                missing_dates += 1
                continue
            item = (date, str(row['RowKey']))
            if len(selected) < args.latest:
                heapq.heappush(selected, item)
            elif item > selected[0]:
                heapq.heapreplace(selected, item)
    if not selected:
        raise ValueError('No dated active uploads found')
    cutoff = selected[0][0]
    names = {name for _, name in selected}
    print(f'Projected scan complete rows={scanned} latest={len(names)} cutoff={cutoff.isoformat()}', flush=True)
    outcome_counts, reasons, sources, failures, raw_counts, backend_rejections = (Counter() for _ in range(6))
    latest_ids = set()
    samples = []
    raw_positive_zero = raw_zero = unknown_raw = 0
    result = {'read_only': True, 'library': args.library, 'scanned_rows': scanned,
              'deleted_rows_excluded': deleted, 'active_without_upload_date': missing_dates,
              'all_active_statuses': dict(statuses), 'requested_latest': args.latest,
              'latest_count': len(names), 'upload_min': cutoff.isoformat(),
              'upload_max': max(selected)[0].isoformat()}
    query = base + " and uploadDate ge '" + cutoff.isoformat() + "'"
    for page in client.query_entities(query, select=['RowKey', 'uploadDate', 'face_status', 'faceCount',
                                                    'processing_metadata'], timeout=15).by_page():
        check_budget()
        for row in page:
            name = str(row['RowKey'])
            if name not in names:
                continue
            latest_ids.add(name)
            status = str(row.get('face_status') or 'missing')
            outcome_counts[status] += 1
            try:
                processing = json.loads(row.get('processing_metadata') or '{}')
                face = processing.get('client_face') or {}
            except (ValueError, TypeError, AttributeError):
                face = {}
            try:
                raw = int(face['rawFaceCount']) if 'rawFaceCount' in face else None
            except (ValueError, TypeError):
                raw = None
            if status != 'no_data':
                continue
            raw_positive_zero += int(raw is not None and raw > 0)
            raw_zero += int(raw == 0)
            unknown_raw += int(raw is None)
            reasons[str(face.get('filteredReason') or 'missing')] += 1
            sources[str(face.get('source') or 'missing')] += 1
            failures[str(face.get('faceFailureStage') or 'missing')] += 1
            raw_counts[str(raw)] += 1
            diagnostic = str(face.get('backendRejectDiagnostic') or '')
            backend_rejections[diagnostic.split(':', 1)[0] or 'missing'] += 1
            if len(samples) < 20:
                samples.append({'filename': name, 'uploadDate': row.get('uploadDate'),
                                'faceCount': row.get('faceCount'), 'client_face': face})
    if latest_ids != names:
        raise ValueError(f'Diagnostics interval missed {len(names - latest_ids)} selected uploads; no complete count')
    result.update(latest_statuses=dict(outcome_counts), no_data_raw_positive=raw_positive_zero,
                  no_data_raw_zero=raw_zero, no_data_raw_unknown=unknown_raw,
                  no_data_filtered_reasons=dict(reasons), no_data_sources=dict(sources),
                  no_data_failure_stages=dict(failures), no_data_raw_counts=dict(raw_counts),
                  no_data_backend_rejections=dict(backend_rejections),
                  no_data_samples=samples, elapsed_seconds=round(time.monotonic()-started, 3),
                  consistency='Live nontransactional scan; outcomes may change during audit')
    with open(args.output, 'w', encoding='utf-8') as handle:
        json.dump(result, handle, indent=2, default=str)
    print(json.dumps({k: v for k, v in result.items() if k != 'no_data_samples'}, indent=2))


if __name__ == '__main__':
    main()