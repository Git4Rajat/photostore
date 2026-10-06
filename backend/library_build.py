"""Resumable, chunked first build of a library's indexes -- built to succeed at millions of photos.

The old full build was ONE streaming pass that had to finish in a single run: ~30-40 minutes at 1M
photos, with no way to resume, so any restart, deploy or scale event threw it all away, and it
needed several multi-GB files to exist at once. This replaces it for a library that has no usable
search database yet (new library, or a schema upgrade):

    1. Publish an EMPTY base database. Search works (on what is indexed so far) from the first chunk.
    2. Scan the partition in RowKey order, ``CHUNK_ROWS`` photos at a time. For each chunk:
         * the sort/access index rows go to small spool files,
         * the search rows are published as a DELTA, and the manifest records ``building.cursor``
           (the last RowKey covered) in the SAME compare-and-swap that advances ``deltaSeq``.
       "Chunk published" and "cursor moved" are therefore one atomic fact: after any crash the next
       run resumes from the cursor and loses at most the chunk in flight.
    3. Assemble the sort/access indexes from the spool files, fold the delta log into one base file
       (``search_db.compact_database``, no table reads), compute the Explore/timeline summaries from
       that file, and clear ``building``.

Memory is O(chunk); disk is the spool (cleaned up at the end) plus the worker's local copy of the
database. Every step is idempotent, so redoing a chunk is harmless.

Tuning: LIBRARY_BUILD_CHUNK_ROWS (20000), LIBRARY_BUILD_MAX_ROWS_PER_SECOND (0 = unthrottled; use it
to keep a huge first build from competing with live traffic for the partition's request budget),
LIBRARY_BUILD_RESTART_AFTER_DAYS (7: an abandoned build older than this starts over).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterator, List, Optional

import index_files
import perf_instrumentation
import search_db
import storage_utils as su
import table_scan

_LOGGER = logging.getLogger(__name__)

CHUNK_ROWS = int(os.getenv('LIBRARY_BUILD_CHUNK_ROWS', '20000'))
MAX_ROWS_PER_SECOND = float(os.getenv('LIBRARY_BUILD_MAX_ROWS_PER_SECOND', '0'))
RESTART_AFTER_DAYS = float(os.getenv('LIBRARY_BUILD_RESTART_AFTER_DAYS', '7'))

# Set by app.py: callable(user_id, db) computing the Explore + timeline summaries from the finished
# database (they need app-level helpers).
FINALIZE_HOOK: Optional[Callable[[str, 'search_db.SearchDatabase'], None]] = None


def _scan_select() -> List[str]:
    return sorted(
        set(su._STREAM_SELECT_FIELDS) | set(su._SORT_INDEX_SOURCE_FIELDS) | set(su._ACCESS_INDEX_SOURCE_FIELDS)
        | {'processing_complete', 'processing_state'}
    )


def _spool_dir(user_id: str, build_id: str) -> str:
    base = index_files.WORK_DIR or os.path.join(tempfile.gettempdir(), 'photostore-index-build')
    digest = hashlib.sha1(user_id.encode('utf-8')).hexdigest()[:12]
    path = os.path.join(base, f'bootstrap-{digest}-{build_id}')
    os.makedirs(path, exist_ok=True)
    return path


def _write_part(path: str, rows: List[Dict]) -> None:
    tmp = f'{path}.{os.getpid()}.tmp'
    with gzip.open(tmp, 'wb', compresslevel=3) as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))
            fh.write(b'\n')
    os.replace(tmp, path)


def _read_part(path: str) -> Iterator[Dict]:
    with gzip.open(path, 'rb') as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _is_true(value) -> bool:
    return str(value).strip().lower() in ('true', '1')


def _building_state(manifest: Dict) -> Optional[Dict]:
    building = manifest.get('building')
    return building if isinstance(building, dict) else None


def bootstrap_needed(user_id: str) -> bool:
    """True when the library has no usable base database, or a first build is in progress."""
    manifest = search_db.load_manifest(user_id)
    if not manifest.get('sourceVersion') or manifest.get('schemaVersion') != search_db.SCHEMA_VERSION:
        return True
    return _building_state(manifest) is not None


def bootstrap_library_build(
    user_id: str, *, on_progress: Optional[Callable[[Dict], None]] = None,
) -> Dict[str, object]:
    """Run (or resume) the chunked first build. Returns a status dict:
    ``exists`` (nothing to do), ``built`` (finished now), ``conflict`` (another writer won; retry)."""
    key = str(user_id or '').strip()
    table = su._CTX.get('metadata_table_client')
    if not key or table is None:
        raise RuntimeError('metadata table not configured')
    search_db.invalidate_manifest_cache(key)
    manifest = search_db.load_manifest(key)
    building = _building_state(manifest)
    valid_base = bool(manifest.get('sourceVersion')) and manifest.get('schemaVersion') == search_db.SCHEMA_VERSION
    if valid_base and building is None:
        return {'status': 'exists'}

    now = datetime.now(timezone.utc)
    if valid_base and building is not None:
        try:
            started = datetime.fromisoformat(str(building.get('startedAt') or ''))
        except ValueError:
            started = None
        if started is not None and now - started > timedelta(days=RESTART_AFTER_DAYS):
            building, valid_base = None, False          # abandoned long ago: start over
    if not valid_base:
        building = {'buildId': uuid.uuid4().hex[:12], 'cursor': '', 'rows': 0, 'chunks': 0, 'startedAt': now.isoformat()}
        search_db.create_empty_base(key, building)
        perf_instrumentation.log_event('library_build_started', user=key, build=building['buildId'])
    else:
        perf_instrumentation.log_event(
            'library_build_resumed', user=key, build=building.get('buildId'), cursor=building.get('cursor'),
            rows=building.get('rows'))

    build_id = str(building['buildId'])
    cursor = str(building.get('cursor') or '')
    total_rows = int(building.get('rows') or 0)
    chunk_index = int(building.get('chunks') or 0)
    spool = _spool_dir(key, build_id)

    base_filter = f"PartitionKey eq '{su._escape_odata(key)}'"
    if cursor:
        base_filter += f" and RowKey gt '{su._escape_odata(cursor)}'"

    upserts: List[Dict] = []
    sort_rows: List[Dict] = []
    access_rows: List[Dict] = []
    scanned = 0
    last_key = cursor
    started_at = time.monotonic()

    def _flush() -> bool:
        nonlocal upserts, sort_rows, access_rows, scanned, total_rows, chunk_index, cursor
        if not scanned:
            return True
        with perf_instrumentation.step('library_build.chunk', user=key, chunk=chunk_index, rows=scanned):
            # Spool files first: a published delta implies its spool parts exist.
            _write_part(os.path.join(spool, f'sort-{chunk_index:06d}.jsonl.gz'), sort_rows)
            _write_part(os.path.join(spool, f'access-{chunk_index:06d}.jsonl.gz'), access_rows)
            new_state = {
                'buildId': build_id, 'cursor': last_key, 'rows': total_rows + scanned, 'chunks': chunk_index + 1,
                'startedAt': building['startedAt'],
            }
            seq = search_db.publish_delta(key, upserts, [], building=new_state)
        if seq is None:
            return False
        total_rows += scanned
        chunk_index += 1
        cursor = last_key
        elapsed = max(1e-6, time.monotonic() - started_at)
        progress = {'rows': total_rows, 'chunks': chunk_index, 'cursor': last_key,
                    'rowsPerSecond': round((total_rows - int(building.get('rows') or 0)) / elapsed)}
        perf_instrumentation.log_event('library_build_progress', user=key, **progress)
        if on_progress is not None:
            try:
                on_progress(progress)
            except Exception:
                _LOGGER.exception('Library build progress callback failed')
        if MAX_ROWS_PER_SECOND > 0:
            wanted = (total_rows - int(building.get('rows') or 0)) / MAX_ROWS_PER_SECOND
            if wanted > elapsed:
                time.sleep(wanted - elapsed)
        upserts, sort_rows, access_rows, scanned = [], [], [], 0
        return True

    with perf_instrumentation.step('library_build.scan', user=key, from_cursor=cursor):
        for row in table_scan.scan_partition(table.query_entities, base_filter, select=_scan_select()):
            name = str(row.get('RowKey') or '').strip()
            if not name:
                continue
            scanned += 1
            last_key = name
            deleted = str(row.get('processing_state') or '').strip().lower() == 'deleted'
            access = su._access_index_row(dict(row))
            if access is not None:
                access_rows.append(access)
            if not deleted:
                sort_row = su._sort_index_row(dict(row))
                if sort_row is not None:
                    sort_rows.append(sort_row)
                # Every non-deleted photo is listed, processed or not: the gallery/Workbench/album covers
                # read this database, and a just-uploaded photo must show up before its OCR/face steps finish.
                record = search_db.row_record(row)
                if record is not None:
                    upserts.append(record)
            if scanned >= CHUNK_ROWS and not _flush():
                return {'status': 'conflict', 'rows': total_rows}
        if not _flush():
            return {'status': 'conflict', 'rows': total_rows}

    return _finalize(key, build_id, spool, chunk_index, total_rows)


def _assemble_rows_index(kind: str, key: str, spool: str, parts: int, *, schema_version: str,
                         blob_name: str, manifest_blob_name: str, version: str) -> bool:
    """Concatenate the spool parts into the standard rows file, publish it and its manifest."""
    names = [os.path.join(spool, f'{kind}-{i:06d}.jsonl.gz') for i in range(parts)]
    if not all(os.path.exists(path) for path in names):
        return False
    container = su._lexical_index_container_name()
    blob_client = su._get_blob_client(container, blob_name) if container else None
    manifest_client = su._get_blob_client(container, manifest_blob_name) if container else None
    if blob_client is None or manifest_client is None:
        return False
    with index_files.workspace() as workdir:
        out_path = os.path.join(workdir, f'{kind}.json.gz')
        header = {'userId': key, 'sourceVersion': version, 'schemaVersion': schema_version, 'updatedAt': version}
        with perf_instrumentation.step(f'library_build.assemble.{kind}', user=key, parts=parts):
            with index_files.RowsWriter(out_path, header) as writer:
                for path in names:
                    for row in _read_part(path):
                        writer.add(row)
            count = writer.count
        su._upload_file_to_blob(
            blob_client, out_path, overwrite=True,
            content_settings=su.BlobContentSettings(content_type='application/json', content_encoding='gzip'),
        )
    su._clear_manifest_dirty_flag(key, kind)
    manifest_client.upload_blob(
        json.dumps({'userId': key, 'sourceVersion': version, 'schemaVersion': schema_version, 'rowCount': count,
                    'dirty': False, 'updatedAt': version}, separators=(',', ':')).encode('utf-8'),
        overwrite=True, content_settings=su.BlobContentSettings(content_type='application/json'),
    )
    return True


def _finalize(key: str, build_id: str, spool: str, chunks: int, total_rows: int) -> Dict[str, object]:
    version = datetime.now(timezone.utc).isoformat()
    result: Dict[str, object] = {'status': 'built', 'rows': total_rows, 'chunks': chunks}

    if su.SORT_INDEX_MAX_ROWS > 0 and total_rows > su.SORT_INDEX_MAX_ROWS:
        # Too big for a browser to use: the gallery pages from the server. Record the manifest only.
        su._publish_skipped_sort_manifest(key, version, total_rows)
        sort_ok = True
        result['sortSkipped'] = True
    else:
        sort_ok = _assemble_rows_index(
            'sort', key, spool, chunks, schema_version=su._SORT_INDEX_SCHEMA_VERSION,
            blob_name=su._sort_index_json_blob_name(key), manifest_blob_name=su._sort_index_manifest_blob_name(key), version=version)
    access_ok = _assemble_rows_index(
        'access', key, spool, chunks, schema_version=su._ACCESS_INDEX_SCHEMA_VERSION,
        blob_name=su._access_index_json_blob_name(key), manifest_blob_name=su._access_index_manifest_blob_name(key), version=version)
    if not sort_ok:        # spool lost (ephemeral disk, restart): fall back to the per-index scans
        _LOGGER.warning('Sort spool missing for %s; rebuilding the sort index from the table', key)
        su.refresh_user_sort_index(key, force_full=True)
        result['sortFallback'] = True
    if not access_ok:
        _LOGGER.warning('Access spool missing for %s; rebuilding the access index from the table', key)
        su.refresh_user_access_index(key, force_full=True)
        result['accessFallback'] = True

    # One base file instead of a long delta log (local disk work, no table reads).
    compacted = search_db.compact_database(key)
    result['compacted'] = bool(compacted)

    # Search is complete; clear the "building" marker (compare-and-swap, lineage-checked).
    _, manifest_name = search_db._blob_names(key)
    manifest_client = search_db._blob_client(manifest_name)
    final = search_db._cas_update_manifest(
        key, manifest_client,
        lambda current: {k: v for k, v in current.items() if k != 'building'}
        if (current.get('building') or {}).get('buildId') == build_id else None)
    if final is None:
        return {'status': 'conflict', 'rows': total_rows}

    # Readiness: the lexical manifest ties the database to this lineage (see search_db.is_current).
    lineage = search_db._lineage(final)
    container = su._lexical_index_container_name()
    su._clear_manifest_dirty_flag(key, 'lexical')
    lexical_client = su._get_blob_client(container, su._lexical_index_manifest_blob_name(key)) if container else None
    if lexical_client is not None:
        lexical_client.upload_blob(
            json.dumps({'userId': key, 'sourceVersion': lineage, 'schemaVersion': su._LEXICAL_INDEX_SCHEMA_VERSION,
                        'rowCount': int(final.get('rowCount') or total_rows), 'dirty': False, 'updatedAt': version},
                       separators=(',', ':')).encode('utf-8'),
            overwrite=True, content_settings=su.BlobContentSettings(content_type='application/json'))
    su.invalidate_user_lexical_index_cache(key)

    # Explore + timeline summaries come from the finished database, not another table scan.
    hook = FINALIZE_HOOK
    if hook is not None:
        try:
            db = search_db.open_database(key)
            if db is not None:
                with perf_instrumentation.step('library_build.summaries', user=key):
                    hook(key, db)
        except Exception:
            _LOGGER.exception('Post-build summaries failed for %s', key)
            result['summariesFailed'] = True

    shutil.rmtree(spool, ignore_errors=True)
    perf_instrumentation.log_event('library_build_done', user=key, **{k: v for k, v in result.items() if isinstance(v, (int, str, bool))})
    return result
