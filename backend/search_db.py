"""Per-library SQLite (FTS5) search database.

Why this exists: server-side search used to load the whole lexical index (every
metadata column of every photo -- ~700MB of JSON at ~130k photos) into the
backend's memory and score every row per query, which is what OOM-ed the 1Gi
backend and why search was switched off (and local/browser search could never
download the index either). Instead:

* ``tools`` (4Gi) builds ONE SQLite file per library from the lexical snapshot it
  already holds: an FTS5 full-text index over exactly the texts the scorer
  reads (filename, effective tags, semantic text incl. OCR, location, camera
  model), plus a compact JSON row per photo for the existing Python scorer.
  Gzipped and stored in Blob Storage (deliberately NOT on the Azure Files
  share -- see below).
* The backend copies that file to LOCAL EPHEMERAL DISK once per replica per
  library version and queries it read-only: bm25-ranked candidate selection in
  SQLite, then the unchanged scoring code runs on a few thousand candidate rows
  instead of the whole library. Memory stays flat; no index lives in RAM.

SQLite wants a real local filesystem (mmap/locking/random reads); SMB shares are
slow and unsafe for that, hence ephemeral disk (SEARCH_DB_DIR, an EmptyDir mount
in production, tempdir otherwise).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import perf_instrumentation
import search_utils
from ordering_utils import metadata_capture_datetime

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 'sqlite-v1'
SEARCH_DB_DIR = os.getenv('SEARCH_DB_DIR', '').strip() or os.path.join(tempfile.gettempdir(), 'photostore-search')
# Cap on candidates pulled per query (bm25-ranked); the scorer then ranks them.
CANDIDATE_LIMIT = int(os.getenv('SEARCH_DB_CANDIDATE_LIMIT', '4000'))
# Total on-disk budget for cached DB files across libraries (oldest evicted).
MAX_CACHE_BYTES = int(os.getenv('SEARCH_DB_MAX_CACHE_MB', '1200')) * 1024 * 1024

_SAFE = re.compile(r'[^A-Za-z0-9._-]')
_TERM = re.compile(r'^[a-z0-9]+$')


# --- row reduction / document text -------------------------------------------

def reduced_row(row: Dict) -> Dict:
    """Compact row that is *scorer-compatible*: same field names/string shapes the
    backend's effective_tags/build_semantic_text/lexical_search_score read, but
    with tags de-duplicated and confidence-filtered, AI predictions cut to a few
    labels, EXIF to the keys search uses and OCR capped (see storage_utils'
    SEARCH_INDEX_* settings)."""
    import storage_utils as su
    out: Dict[str, object] = {'RowKey': row.get('RowKey')}
    for field in su._SEARCH_SLIM_PASSTHROUGH_FIELDS + ('latitude', 'longitude'):
        value = row.get(field)
        if value not in (None, '', '[]'):
            out[field] = value
    if out.get('latitude') and out.get('longitude'):
        out['latitude'] = out['longitude'] = '1'  # search only tests presence
    names = su._json_list(row.get('peopleNames'))
    if names:
        out['peopleNames'] = json.dumps(names, ensure_ascii=False, separators=(',', ':'))
    subject, others = su._search_slim_tags(row)
    if subject:
        out['subjectTags'] = json.dumps(subject, ensure_ascii=False, separators=(',', ':'))
    if others:
        out['tags'] = json.dumps(others, ensure_ascii=False, separators=(',', ':'))
    labels = su._search_prediction_labels(row.get('processing_metadata'), set(subject) | set(others))
    if labels:
        out['processing_metadata'] = json.dumps(
            {'client_ai_vision': {'predictions': [{'label': label, 'score': 1.0} for label in labels]}},
            ensure_ascii=False, separators=(',', ':'),
        )
    for field in ('uploadDate', 'upload_started_at', 'last_processing_update'):
        if row.get(field):
            out['uploadDate'] = row[field]
            break
    if row.get('exifData'):
        exif = su._slim_exif_for_search(row.get('exifData'))
        if exif != '{}':
            out['exifData'] = exif
    ocr = ' '.join(str(row.get('ocrText') or '').split())
    if ocr:
        out['ocrText'] = ocr[:su.SEARCH_INDEX_OCR_MAX_CHARS]
    return out


def document_text(filename: str, row: Dict) -> str:
    """Everything lexical_search_score can match a query token against."""
    norm = search_utils._normalize_token
    exif_model = ''
    try:
        exif_model = str(json.loads(row.get('exifData') or '{}').get('Model') or '')
    except Exception:
        pass
    location = ' '.join(str(row.get(f) or '') for f in ('address', 'locationCity', 'locationRegion', 'locationCountry'))
    return ' '.join(part for part in (
        norm(filename),
        ' '.join(search_utils.effective_tags(row)),
        search_utils.build_semantic_text(filename, row),
        norm(location),
        norm(exif_model),
    ) if part)


def _location_terms(rows: Iterable[Dict]) -> List[str]:
    terms = set()
    for row in rows:
        for field in ('locationCity', 'locationRegion', 'locationCountry', 'address'):
            phrase = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(row.get(field) or '').lower())).strip()
            if not phrase:
                continue
            for part in phrase.split(' '):
                if len(part) >= 3:
                    terms.add(part)
            if field != 'address':  # full street addresses are per-photo noise
                terms.add(phrase)
    return sorted(terms, key=len, reverse=True)


# --- build --------------------------------------------------------------------

def build_database(rows: Iterable[Dict], path: str) -> int:
    """Write the SQLite file at ``path``. Returns the number of photos indexed."""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            '''
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=8192;
            CREATE TABLE rows(id INTEGER PRIMARY KEY, filename TEXT NOT NULL, capture_day INTEGER, row_json TEXT NOT NULL);
            CREATE TABLE row_people(person_id TEXT NOT NULL, id INTEGER NOT NULL);
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE fts USING fts5(doc, content='',
                tokenize='unicode61 remove_diacritics 2', prefix='3 4');
            '''
        )
        count = 0
        location_rows: List[Dict] = []
        batch_rows: List[Tuple] = []
        batch_fts: List[Tuple] = []
        batch_people: List[Tuple] = []
        for row in rows:
            filename = str(row.get('RowKey') or '').strip()
            if not filename:
                continue
            count += 1
            slim = reduced_row(row)
            captured = metadata_capture_datetime(row)
            day = captured.astimezone(timezone.utc).date().toordinal() if captured else None
            batch_rows.append((count, filename, day, json.dumps(slim, ensure_ascii=False, separators=(',', ':'))))
            batch_fts.append((count, document_text(filename, slim)))
            try:
                for pid in json.loads(row.get('peopleIds') or '[]'):
                    batch_people.append((str(pid), count))
            except Exception:
                pass
            location_rows.append({k: slim.get(k) for k in ('locationCity', 'locationRegion', 'locationCountry', 'address')})
            if len(batch_rows) >= 2000:
                _flush(conn, batch_rows, batch_fts, batch_people)
        _flush(conn, batch_rows, batch_fts, batch_people)
        conn.execute('CREATE INDEX rows_capture ON rows(capture_day)')
        conn.execute('CREATE INDEX rows_filename ON rows(filename)')
        conn.execute('CREATE INDEX row_people_pid ON row_people(person_id)')
        conn.execute('INSERT INTO meta VALUES(?, ?)', ('location_terms', json.dumps(_location_terms(location_rows))))
        conn.execute('INSERT INTO meta VALUES(?, ?)', ('schema', SCHEMA_VERSION))
        conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
        conn.commit()
        return count
    finally:
        conn.close()


def _flush(conn, batch_rows, batch_fts, batch_people) -> None:
    if batch_rows:
        conn.executemany('INSERT INTO rows(id, filename, capture_day, row_json) VALUES(?,?,?,?)', batch_rows)
        conn.executemany('INSERT INTO fts(rowid, doc) VALUES(?,?)', batch_fts)
    if batch_people:
        conn.executemany('INSERT INTO row_people(person_id, id) VALUES(?,?)', batch_people)
    batch_rows.clear()
    batch_fts.clear()
    batch_people.clear()


# --- query --------------------------------------------------------------------

def match_terms(tokens: Dict[str, List[str]]) -> List[str]:
    """All normalized terms a photo could match (query tokens, expansions and
    their singular/plural variants) -- the same set lexical_search_score tests."""
    seen: List[str] = []
    for source in ('all', 'expanded', 'subject', 'location', 'modifiers', 'required_object'):
        for token in tokens.get(source, []) or []:
            for variant in search_utils._token_variants(token):
                norm = search_utils._normalize_token(variant)
                for part in norm.split(' '):
                    if part and _TERM.match(part) and part not in seen:
                        seen.append(part)
    return seen


def fts_expression(terms: Sequence[str]) -> str:
    # _contains_related_term allows `token[a-z0-9]*` for tokens of 4+ chars.
    return ' OR '.join(f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in terms)


class SearchDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._location_terms: Optional[List[str]] = None

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True, check_same_thread=False)
            conn.execute('PRAGMA cache_size=-16000')  # ~16MB page cache per thread
            self._local.conn = conn
        return conn

    def location_terms(self) -> List[str]:
        if self._location_terms is None:
            row = self._conn().execute("SELECT value FROM meta WHERE key='location_terms'").fetchone()
            self._location_terms = json.loads(row[0]) if row else []
        return self._location_terms

    def candidates(
        self, terms: Sequence[str], *, person_ids: Sequence[str] = (),
        capture_start_day: Optional[int] = None, capture_end_day: Optional[int] = None,
        limit: int = CANDIDATE_LIMIT,
    ) -> List[Tuple[str, Dict]]:
        """bm25-ranked candidate (filename, reduced_row) pairs. Rows of explicitly
        named people are always included, even if their text doesn't match."""
        conn = self._conn()
        ids: List[int] = []
        range_sql, range_args = '', []
        if capture_start_day is not None:
            range_sql += ' AND rows.capture_day >= ?'
            range_args.append(capture_start_day)
        if capture_end_day is not None:
            range_sql += ' AND rows.capture_day <= ?'
            range_args.append(capture_end_day)
        if terms:
            try:
                cur = conn.execute(
                    'SELECT fts.rowid FROM fts JOIN rows ON rows.id = fts.rowid '
                    f'WHERE fts MATCH ?{range_sql} ORDER BY fts.rank LIMIT ?',
                    [fts_expression(terms), *range_args, int(limit)],
                )
                ids.extend(r[0] for r in cur)
            except sqlite3.OperationalError:
                _LOGGER.warning('FTS query failed for terms=%s', list(terms)[:8], exc_info=True)
        if person_ids:
            marks = ','.join('?' * len(person_ids))
            cur = conn.execute(
                f'SELECT DISTINCT row_people.id FROM row_people JOIN rows ON rows.id = row_people.id '
                f'WHERE row_people.person_id IN ({marks}){range_sql} LIMIT ?',
                [*person_ids, *range_args, int(limit)],
            )
            ids.extend(r[0] for r in cur)
        ids = list(dict.fromkeys(ids))
        out: List[Tuple[str, Dict]] = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ','.join('?' * len(chunk))
            for _id, filename, row_json in conn.execute(
                f'SELECT id, filename, row_json FROM rows WHERE id IN ({marks})', chunk,
            ):
                out.append((filename, json.loads(row_json)))
        return out

    def row_count(self) -> int:
        return int(self._conn().execute('SELECT COUNT(*) FROM rows').fetchone()[0])


# --- blob storage + local ephemeral cache -------------------------------------

def _blob_names(user_id: str) -> Tuple[str, str]:
    import storage_utils as su
    key = su._vector_index_blob_key(user_id)
    return f'{key}-searchdb.sqlite.gz', f'{key}-searchdb.json'


def _blob_client(name: str):
    import storage_utils as su
    return su._get_blob_client(su._lexical_index_container_name(), name)


def load_manifest(user_id: str) -> Dict:
    _, manifest_name = _blob_names(user_id)
    client = _blob_client(manifest_name)
    if client is None:
        return {}
    try:
        parsed = json.loads(client.download_blob().readall().decode('utf-8'))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def is_current(user_id: str, lexical_source_version: str) -> bool:
    manifest = load_manifest(user_id)
    return bool(
        manifest.get('sourceVersion')
        and manifest.get('sourceVersion') == lexical_source_version
        and manifest.get('schemaVersion') == SCHEMA_VERSION
    )


def write_for_snapshot(user_id: str, snapshot) -> bool:
    """Build the DB from an in-memory lexical snapshot (tools role) and upload
    it. Best-effort: a failure only means search keeps reporting 'building'."""
    data_name, manifest_name = _blob_names(user_id)
    data_client, manifest_client = _blob_client(data_name), _blob_client(manifest_name)
    if data_client is None or manifest_client is None:
        return False
    workdir = tempfile.mkdtemp(prefix='searchdb-build-')
    try:
        db_path = os.path.join(workdir, 'search.sqlite')
        with perf_instrumentation.span('searchdb.build', user=user_id, rows=len(snapshot.rows)):
            count = build_database(snapshot.rows, db_path)
        gz_path = db_path + '.gz'
        with perf_instrumentation.span('searchdb.gzip', user=user_id):
            with open(db_path, 'rb') as src, gzip.open(gz_path, 'wb', compresslevel=5) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        perf_instrumentation.log_event(
            'searchdb_built', user=user_id, rows=count,
            sqlite_mb=round(os.path.getsize(db_path) / 1048576, 1), gz_mb=round(os.path.getsize(gz_path) / 1048576, 1),
        )
        from azure.storage.blob import ContentSettings
        with perf_instrumentation.span('searchdb.upload', user=user_id):
            with open(gz_path, 'rb') as fh:
                data_client.upload_blob(fh, overwrite=True, content_settings=ContentSettings(content_type='application/gzip'))
        manifest_client.upload_blob(
            json.dumps({
                'userId': user_id, 'sourceVersion': snapshot.source_version, 'schemaVersion': SCHEMA_VERSION,
                'rowCount': count, 'updatedAt': snapshot.updated_at,
            }, separators=(',', ':')).encode('utf-8'),
            overwrite=True, content_settings=ContentSettings(content_type='application/json'),
        )
        return True
    except Exception:
        _LOGGER.exception('Search DB build/upload failed for user %s', user_id)
        return False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def delete_for_user(user_id: str) -> None:
    for name in _blob_names(user_id):
        client = _blob_client(name)
        if client is not None:
            try:
                client.delete_blob()
            except Exception:
                pass
    _evict_user(user_id)


def _safe(value: str) -> str:
    return _SAFE.sub('_', value)


def _local_path(user_id: str, source_version: str) -> str:
    digest = hashlib.sha1(source_version.encode('utf-8')).hexdigest()[:12]
    return os.path.join(SEARCH_DB_DIR, f'{_safe(user_id)}-{digest}.sqlite')


def _evict_user(user_id: str, keep: Optional[str] = None) -> None:
    prefix = f'{_safe(user_id)}-'
    try:
        for name in os.listdir(SEARCH_DB_DIR):
            if name.startswith(prefix) and os.path.join(SEARCH_DB_DIR, name) != keep:
                try:
                    os.remove(os.path.join(SEARCH_DB_DIR, name))
                except OSError:
                    pass
    except OSError:
        pass


def _enforce_budget(keep: str) -> None:
    try:
        files = [os.path.join(SEARCH_DB_DIR, n) for n in os.listdir(SEARCH_DB_DIR) if n.endswith('.sqlite')]
        files.sort(key=lambda p: os.path.getmtime(p))
        total = sum(os.path.getsize(p) for p in files)
        for path in files:
            if total <= MAX_CACHE_BYTES:
                break
            if path != keep:
                total -= os.path.getsize(path)
                os.remove(path)
    except OSError:
        pass


_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.Lock] = {}
_OPEN: Dict[str, SearchDatabase] = {}


def _user_lock(user_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(user_id, threading.Lock())


def open_database(user_id: str, *, allow_download: bool = True) -> Optional[SearchDatabase]:
    """The library's current search DB on local ephemeral disk, downloading and
    unpacking it (streamed, never fully in memory) if this replica lacks the
    current version. None if no current DB exists yet."""
    manifest = load_manifest(user_id)
    version = str(manifest.get('sourceVersion') or '')
    if not version or manifest.get('schemaVersion') != SCHEMA_VERSION:
        return None
    path = _local_path(user_id, version)
    with _LOCKS_GUARD:
        existing = _OPEN.get(path)
    if existing is not None and os.path.exists(path):
        return existing
    if not os.path.exists(path):
        if not allow_download:
            return None
        with _user_lock(user_id):
            if not os.path.exists(path):
                if not _download(user_id, path):
                    return None
    db = SearchDatabase(path)
    with _LOCKS_GUARD:
        _OPEN[path] = db
        for stale in [p for p in _OPEN if p != path and os.path.basename(p).startswith(f'{_safe(user_id)}-')]:
            _OPEN.pop(stale, None)
    _evict_user(user_id, keep=path)
    return db


def _download(user_id: str, path: str) -> bool:
    data_name, _ = _blob_names(user_id)
    client = _blob_client(data_name)
    if client is None:
        return False
    os.makedirs(SEARCH_DB_DIR, exist_ok=True)
    tmp_gz = f'{path}.{os.getpid()}.{threading.get_ident()}.gz.tmp'
    tmp_db = f'{path}.{os.getpid()}.{threading.get_ident()}.db.tmp'
    try:
        with perf_instrumentation.span('searchdb.download', user=user_id):
            with open(tmp_gz, 'wb') as fh:
                client.download_blob().readinto(fh)
        with perf_instrumentation.span('searchdb.unpack', user=user_id):
            with gzip.open(tmp_gz, 'rb') as src, open(tmp_db, 'wb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        os.replace(tmp_db, path)
        _enforce_budget(keep=path)
        perf_instrumentation.log_event('searchdb_ready', user=user_id, mb=round(os.path.getsize(path) / 1048576, 1))
        return True
    except Exception:
        _LOGGER.exception('Search DB download failed for user %s', user_id)
        return False
    finally:
        for tmp in (tmp_gz, tmp_db):
            try:
                os.remove(tmp)
            except OSError:
                pass


_WARM_LAST: Dict[str, float] = {}
WARM_COOLDOWN_SECONDS = float(os.getenv('SEARCH_DB_WARM_COOLDOWN_SECONDS', '120'))


def warm_async(user_id: str) -> bool:
    """Session start: pull the current DB onto this replica's ephemeral disk in
    the background so the first search doesn't pay for the download."""
    now = time.monotonic()
    with _LOCKS_GUARD:
        last = _WARM_LAST.get(user_id)
        if last is not None and now - last < WARM_COOLDOWN_SECONDS:
            return False
        _WARM_LAST[user_id] = now

    def _run() -> None:
        try:
            open_database(user_id, allow_download=True)
        except Exception:
            _LOGGER.exception('Search DB warm failed for user %s', user_id)

    threading.Thread(target=_run, name='searchdb-warm', daemon=True).start()
    return True
