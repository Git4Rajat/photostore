"""Per-library SQLite (FTS5) search + gallery database.

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

The same file is the backend's read model for every other library-sized query
(gallery list/filter, "on this day" suggestions, place typeahead): `rows` carries
the gallery columns, so those endpoints are SQL with LIMIT/OFFSET and flat
memory instead of loading the listing blob (130k rows) into the 1Gi backend.
Rating/like edits show up here on the next index build (a few minutes); the
photos actually returned are always re-read fresh from the table.

SQLite wants a real local filesystem (mmap/locking/random reads); SMB shares are
slow and unsafe for that, hence ephemeral disk (SEARCH_DB_DIR, an EmptyDir mount
in production, tempdir otherwise).
"""
from __future__ import annotations

import contextlib
import fcntl
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
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import perf_instrumentation
import search_utils
from ordering_utils import metadata_capture_datetime, metadata_upload_datetime

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 'sqlite-v6'  # v6: indexed representative covers for timeline periods
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


def _as_int(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> Optional[float]:
    try:
        text = str(value if value is not None else '').strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _cover_rank(filename: str) -> int:
    """Stable pseudo-random ordering for period covers across rebuilds/replicas."""
    return int.from_bytes(hashlib.sha256(filename.encode('utf-8')).digest()[:8], 'big') & 0x7fffffffffffffff


def _collect_location_terms(into: set, row: Dict) -> None:
    for field in ('locationCity', 'locationRegion', 'locationCountry', 'address'):
        phrase = re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', str(row.get(field) or '').lower())).strip()
        if not phrase:
            continue
        for part in phrase.split(' '):
            if len(part) >= 3:
                into.add(part)
        if field != 'address':  # full street addresses are per-photo noise
            into.add(phrase)


def row_record(row: Dict) -> Optional[Dict]:
    """Everything the database stores for one photo (shared by the full build and by delta
    updates, so both produce byte-identical rows): the reduced JSON, gallery columns, people ids,
    the full-text document and the place-name terms. None when the row has no filename."""
    filename = str(row.get('RowKey') or '').strip()
    if not filename:
        return None
    slim = reduced_row(row)
    captured = metadata_capture_datetime(row)
    uploaded = metadata_upload_datetime(row)
    people: List[str] = []
    try:
        people = [str(pid) for pid in json.loads(row.get('peopleIds') or '[]')]
    except Exception:
        pass
    terms: set = set()
    _collect_location_terms(terms, slim)
    return {
        'filename': filename,
        'row_json': json.dumps(slim, ensure_ascii=False, separators=(',', ':')),
        'capture_day': captured.astimezone(timezone.utc).date().toordinal() if captured else None,
        'capture_ts': captured.timestamp() if captured else None,
        'upload_ts': uploaded.timestamp() if uploaded else None,
        'capture_year': captured.year if captured else None,
        'capture_md': captured.month * 100 + captured.day if captured else None,
        'rating': _as_int(row.get('rating')), 'likes': _as_int(row.get('likes')),
        'has_likes': 1 if _as_int(row.get('likes')) > 0 else 0,
        'cover_rank': _cover_rank(filename),
        'lat': _as_float(row.get('latitude')), 'lon': _as_float(row.get('longitude')),
        'people': people,
        'doc': document_text(filename, slim),
        'terms': sorted(terms),
    }


# --- build --------------------------------------------------------------------

class DatabaseBuilder:
    """Streaming SQLite writer: add(row) per photo, finish() once. Holds one
    2000-row batch and a set of place-name terms -- nothing else -- so building
    the database never needs the library in memory."""

    BATCH = 2000

    def __init__(self, path: str) -> None:
        if os.path.exists(path):
            os.remove(path)
        self.path = path
        self.count = 0
        self._terms: set = set()
        self._rows: List[Tuple] = []
        self._fts: List[Tuple] = []
        self._people: List[Tuple] = []
        self._conn = sqlite3.connect(path)
        self._conn.executescript(
            '''
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=8192;
            CREATE TABLE rows(
                id INTEGER PRIMARY KEY, filename TEXT NOT NULL, capture_day INTEGER, row_json TEXT NOT NULL,
                capture_ts REAL, upload_ts REAL, capture_year INTEGER, capture_md INTEGER,
                rating INTEGER NOT NULL DEFAULT 0, likes INTEGER NOT NULL DEFAULT 0,
                has_likes INTEGER NOT NULL DEFAULT 0, cover_rank INTEGER NOT NULL DEFAULT 0, lat REAL, lon REAL
            );
            CREATE TABLE row_people(person_id TEXT NOT NULL, id INTEGER NOT NULL);
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE fts USING fts5(doc, content='',
                tokenize='unicode61 remove_diacritics 2', prefix='3 4');
            '''
        )

    def add(self, row: Dict) -> None:
        record = row_record(row)
        if record is None:
            return
        self.count += 1
        self._rows.append((
            self.count, record['filename'], record['capture_day'], record['row_json'], record['capture_ts'],
            record['upload_ts'], record['capture_year'], record['capture_md'], record['rating'], record['likes'], record['has_likes'], record['cover_rank'],
            record['lat'], record['lon'],
        ))
        self._fts.append((self.count, record['doc']))
        self._people.extend((pid, self.count) for pid in record['people'])
        self._terms.update(record['terms'])
        if len(self._rows) >= self.BATCH:
            self._flush()

    def _flush(self) -> None:
        if self._rows:
            self._conn.executemany(
                'INSERT INTO rows(id, filename, capture_day, row_json, capture_ts, upload_ts, capture_year, capture_md, rating, likes, has_likes, cover_rank, lat, lon) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', self._rows)
            self._conn.executemany('INSERT INTO fts(rowid, doc) VALUES(?,?)', self._fts)
        if self._people:
            self._conn.executemany('INSERT INTO row_people(person_id, id) VALUES(?,?)', self._people)
        self._rows, self._fts, self._people = [], [], []

    def finish(self) -> int:
        self._flush()
        conn = self._conn
        conn.execute('CREATE INDEX rows_capture ON rows(capture_day)')
        conn.execute('CREATE INDEX rows_filename ON rows(filename)')
        conn.execute('CREATE INDEX rows_capture_md ON rows(capture_md)')
        conn.execute('CREATE INDEX rows_capture_ts ON rows(capture_ts)')
        conn.execute('CREATE INDEX rows_upload_ts ON rows(upload_ts)')
        conn.execute('CREATE INDEX rows_rating ON rows(rating, likes)')
        conn.execute('CREATE INDEX rows_rating_order ON rows(rating DESC, likes DESC, capture_ts DESC, filename ASC)')
        conn.execute('CREATE INDEX rows_likes_order ON rows(likes DESC, rating DESC, capture_ts DESC, filename ASC)')
        conn.execute('CREATE INDEX rows_capture_range_order ON rows(capture_day, capture_ts DESC, filename ASC)')
        conn.execute('CREATE INDEX rows_rating_capture_order ON rows(rating, capture_ts DESC, filename ASC)')
        conn.execute('CREATE INDEX rows_likes_capture_order ON rows(likes, capture_ts DESC, filename ASC)')
        conn.execute('CREATE INDEX rows_period_cover ON rows(capture_year, capture_md, has_likes DESC, cover_rank)')
        conn.execute('CREATE INDEX rows_year_cover ON rows(capture_year, has_likes DESC, cover_rank)')
        conn.execute('CREATE INDEX row_people_pid ON row_people(person_id)')
        conn.execute('INSERT INTO meta VALUES(?, ?)', (
            'location_terms', json.dumps(sorted(self._terms, key=len, reverse=True))))
        conn.execute('INSERT INTO meta VALUES(?, ?)', ('schema', SCHEMA_VERSION))
        conn.execute("INSERT INTO fts(fts) VALUES('optimize')")
        conn.commit()
        conn.close()
        return self.count

    def abort(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


def build_database(rows: Iterable[Dict], path: str) -> int:
    """Write the SQLite file at ``path`` from any iterable of rows."""
    builder = DatabaseBuilder(path)
    try:
        for row in rows:
            builder.add(row)
        return builder.finish()
    except Exception:
        builder.abort()
        raise


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


def _range_clause(start_day: Optional[int], end_day: Optional[int]) -> Tuple[str, List[object]]:
    clauses, args = [], []
    if start_day is not None:
        clauses.append('capture_day >= ?')
        args.append(start_day)
    if end_day is not None:
        clauses.append('capture_day <= ?')
        args.append(end_day)
    return (' WHERE ' + ' AND '.join(clauses), args) if clauses else ('', args)


class SearchDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self._location_terms: Optional[List[str]] = None
        self._seq: Optional[int] = None
        self._timeline: Optional[Tuple[int, Dict]] = None

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True, check_same_thread=False)
            conn.execute('PRAGMA cache_size=-16000')  # ~16MB page cache per thread
            self._local.conn = conn
        return conn

    def applied_seq(self) -> int:
        """Sequence number of the last delta applied to this local copy (0 = the base as built)."""
        if self._seq is None:
            row = self._conn().execute("SELECT value FROM meta WHERE key='delta_seq'").fetchone()
            self._seq = int(row[0]) if row else 0
        return self._seq

    def apply_delta(self, delta: Dict) -> None:
        """Apply one published delta (upserted photos + removed filenames) in place.

        One write transaction on a WAL-mode file, so concurrent readers keep serving the previous
        snapshot until it commits. A replaced photo's old row is removed from the contentless FTS
        index by re-deriving the exact document that was indexed for it."""
        upserts = delta.get('upserts') or []
        deletes = [str(n) for n in (delta.get('deletes') or [])]
        conn = sqlite3.connect(self.path, timeout=60)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('BEGIN IMMEDIATE')
            touched = list(dict.fromkeys([*deletes, *(str(u['filename']) for u in upserts)]))
            for start in range(0, len(touched), 500):
                chunk = touched[start:start + 500]
                marks = ','.join('?' * len(chunk))
                for row_id, filename, row_json in conn.execute(
                    f'SELECT id, filename, row_json FROM rows WHERE filename IN ({marks})', chunk,
                ).fetchall():
                    try:
                        old_doc = document_text(filename, json.loads(row_json))
                        conn.execute("INSERT INTO fts(fts, rowid, doc) VALUES('delete', ?, ?)", (row_id, old_doc))
                    except Exception:
                        _LOGGER.warning('Could not drop FTS entry for %s', filename, exc_info=True)
                    conn.execute('DELETE FROM row_people WHERE id = ?', (row_id,))
                    conn.execute('DELETE FROM rows WHERE id = ?', (row_id,))
            next_id = int(conn.execute('SELECT COALESCE(MAX(id), 0) + 1 FROM rows').fetchone()[0])
            terms: set = set()
            for upsert in upserts:
                conn.execute(
                    'INSERT INTO rows(id, filename, capture_day, row_json, capture_ts, upload_ts, capture_year, capture_md, '
                    'rating, likes, has_likes, cover_rank, lat, lon) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (next_id, upsert['filename'], upsert.get('capture_day'), upsert['row_json'], upsert.get('capture_ts'),
                     upsert.get('upload_ts'), upsert.get('capture_year'), upsert.get('capture_md'),
                     int(upsert.get('rating') or 0), int(upsert.get('likes') or 0), int(upsert.get('has_likes') or 0),
                     int(upsert.get('cover_rank') or 0),
                     upsert.get('lat'), upsert.get('lon')))
                conn.execute('INSERT INTO fts(rowid, doc) VALUES(?, ?)', (next_id, upsert['doc']))
                conn.executemany('INSERT INTO row_people(person_id, id) VALUES(?, ?)',
                                 [(str(pid), next_id) for pid in upsert.get('people') or []])
                terms.update(upsert.get('terms') or [])
                next_id += 1
            if terms:
                row = conn.execute("SELECT value FROM meta WHERE key='location_terms'").fetchone()
                merged = set(json.loads(row[0])) if row else set()
                merged |= terms
                conn.execute("INSERT OR REPLACE INTO meta VALUES('location_terms', ?)",
                             (json.dumps(sorted(merged, key=len, reverse=True)),))
            conn.execute("INSERT OR REPLACE INTO meta VALUES('delta_seq', ?)", (str(int(delta['seq'])),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._seq = int(delta['seq'])
        self._location_terms = None
        self._timeline = None

    def timeline_summary(self, *, today=None) -> Dict:
        """Year/month/day photo counts for the timeline, from the stored capture days (same shape
        and rules as TimelineAccumulator: undated and future-dated photos are counted aside).
        Cached until the next delta, so it always reflects new uploads."""
        from datetime import date as _date
        today = today or datetime.now(timezone.utc).date()
        seq = self.applied_seq()
        cached = self._timeline
        if cached is not None and cached[0] == seq and cached[1].get('today') == today.isoformat():
            return cached[1]
        conn = self._conn()
        total = int(conn.execute('SELECT COUNT(*) FROM rows').fetchone()[0])
        undated = int(conn.execute('SELECT COUNT(*) FROM rows WHERE capture_day IS NULL').fetchone()[0])
        years: Dict[str, Dict] = {}
        future = 0
        first = last = None
        for ordinal, count in conn.execute('SELECT capture_day, COUNT(*) FROM rows WHERE capture_day IS NOT NULL GROUP BY capture_day'):
            day = _date.fromordinal(int(ordinal))
            if day > today:
                future += int(count)
                continue
            year_bucket = years.setdefault(f'{day.year:04d}', {'count': 0, 'months': {}})
            month_bucket = year_bucket['months'].setdefault(f'{day.month:02d}', {'count': 0, 'days': {}})
            year_bucket['count'] += int(count)
            month_bucket['count'] += int(count)
            month_bucket['days'][f'{day.day:02d}'] = int(count)
            first = day if first is None or day < first else first
            last = day if last is None or day > last else last
        cumulative: Dict[str, int] = {}
        running = 0
        for year_key in sorted(years):
            running += years[year_key]['count']
            cumulative[year_key] = running
        # Representative covers are point-like reads over period-prefixed
        # indexes. Positive likes win; cover_rank then picks a stable,
        # pseudo-random photo so unliked periods do not all show their first
        # filename and covers do not reshuffle between replicas.
        today_ordinal = today.toordinal()
        for year_key, year_bucket in years.items():
            year = int(year_key)
            row = conn.execute(
                'SELECT filename FROM rows WHERE capture_year = ? AND capture_day <= ? '
                'ORDER BY has_likes DESC, cover_rank LIMIT 1',
                (year, today_ordinal),
            ).fetchone()
            if row:
                year_bucket['coverFilename'] = str(row[0])
            for month_key, month_bucket in year_bucket['months'].items():
                month = int(month_key)
                row = conn.execute(
                    'SELECT filename FROM rows WHERE capture_year = ? AND capture_md BETWEEN ? AND ? '
                    'AND capture_day <= ? ORDER BY has_likes DESC, cover_rank LIMIT 1',
                    (year, month * 100 + 1, month * 100 + 31, today_ordinal),
                ).fetchone()
                if row:
                    month_bucket['coverFilename'] = str(row[0])
        summary = {
            'years': years, 'cumulativeByYear': cumulative,
            'firstDate': first.isoformat() if first else None, 'lastDate': last.isoformat() if last else None,
            'today': today.isoformat(), 'undatedCount': undated, 'futureCount': future, 'totalCount': total,
        }
        self._timeline = (seq, summary)
        return summary

    def location_terms(self) -> List[str]:
        if self._location_terms is None:
            row = self._conn().execute("SELECT value FROM meta WHERE key='location_terms'").fetchone()
            self._location_terms = json.loads(row[0]) if row else []
        return self._location_terms

    @staticmethod
    def _group_sql(person_groups: Sequence[Sequence[str]]) -> Tuple[str, List[object]]:
        """Every queried person must appear on the photo ("alice and bob" means both), one EXISTS per group."""
        sql, args = '', []
        for group in person_groups:
            ids = [str(pid) for pid in group if pid]
            if not ids:
                continue
            sql += f' AND EXISTS (SELECT 1 FROM row_people rp WHERE rp.id = rows.id AND rp.person_id IN ({",".join("?" * len(ids))}))'
            args.extend(ids)
        return sql, args

    def _set_sql(
        self, terms: Sequence[str], person_ids: Sequence[str], person_groups: Sequence[Sequence[str]],
        start_day: Optional[int], end_day: Optional[int],
    ) -> Tuple[str, List[object]]:
        """WHERE clause (over ``rows``) for the full result set: photos whose text matches OR who are
        one of the queried people, that carry every queried person, inside the date range."""
        parts, args = [], []
        if terms:
            parts.append('rows.id IN (SELECT rowid FROM fts WHERE fts MATCH ?)')
            args.append(fts_expression(terms))
        if person_ids:
            parts.append(f'rows.id IN (SELECT id FROM row_people WHERE person_id IN ({",".join("?" * len(person_ids))}))')
            args.extend(person_ids)
        if not parts:
            return '0', []
        sql = '(' + ' OR '.join(parts) + ')'
        group_sql, group_args = self._group_sql(person_groups)
        sql += group_sql
        args.extend(group_args)
        if start_day is not None:
            sql += ' AND rows.capture_day >= ?'
            args.append(start_day)
        if end_day is not None:
            sql += ' AND rows.capture_day <= ?'
            args.append(end_day)
        return sql, args

    def count_matches(
        self, terms: Sequence[str], *, person_ids: Sequence[str] = (), person_groups: Sequence[Sequence[str]] = (),
        capture_start_day: Optional[int] = None, capture_end_day: Optional[int] = None,
    ) -> int:
        """Exact size of the whole result set (not just the ranked window)."""
        sql, args = self._set_sql(terms, person_ids, person_groups, capture_start_day, capture_end_day)
        try:
            return int(self._conn().execute(f'SELECT COUNT(*) FROM rows WHERE {sql}', args).fetchone()[0])
        except sqlite3.OperationalError:
            _LOGGER.warning('FTS count failed for terms=%s', list(terms)[:8], exc_info=True)
            return 0

    def tail_rows(
        self, terms: Sequence[str], *, person_ids: Sequence[str] = (), person_groups: Sequence[Sequence[str]] = (),
        capture_start_day: Optional[int] = None, capture_end_day: Optional[int] = None,
        exclude_ids: Sequence[int] = (), offset: int = 0, limit: int = 100,
    ) -> List[Tuple[str, Dict]]:
        """A page of the result set BEYOND the ranked window: everything the window did not cover,
        newest capture first (relevance ranking stops at the window; past it the order is simply
        recency, which is stable and cheap to page through at any depth)."""
        sql, args = self._set_sql(terms, person_ids, person_groups, capture_start_day, capture_end_day)
        if exclude_ids:
            sql += ' AND rows.id NOT IN (SELECT value FROM json_each(?))'
            args.append(json.dumps([int(i) for i in exclude_ids]))
        try:
            cur = self._conn().execute(
                f'SELECT rows.filename, rows.row_json FROM rows WHERE {sql} '
                'ORDER BY rows.capture_ts DESC, rows.filename ASC LIMIT ? OFFSET ?',
                [*args, int(limit), max(0, int(offset))])
            return [(filename, json.loads(row_json)) for filename, row_json in cur]
        except sqlite3.OperationalError:
            _LOGGER.warning('Tail query failed for terms=%s', list(terms)[:8], exc_info=True)
            return []

    def candidates(self, terms: Sequence[str], **kwargs) -> List[Tuple[str, Dict]]:
        return [(filename, row) for _id, filename, row in self.candidates_with_ids(terms, **kwargs)]

    def candidates_with_ids(
        self, terms: Sequence[str], *, person_ids: Sequence[str] = (),
        person_groups: Sequence[Sequence[str]] = (),
        capture_start_day: Optional[int] = None, capture_end_day: Optional[int] = None,
        limit: int = CANDIDATE_LIMIT,
    ) -> List[Tuple[int, str, Dict]]:
        """The ranked WINDOW: up to ``limit`` bm25-ranked (id, filename, reduced_row) triples, plus
        rows of explicitly named people (newest first), always included even if their text doesn't
        match. Rows missing a queried person are excluded here, not later."""
        conn = self._conn()
        ids: List[int] = []
        range_sql, range_args = '', []
        if capture_start_day is not None:
            range_sql += ' AND rows.capture_day >= ?'
            range_args.append(capture_start_day)
        if capture_end_day is not None:
            range_sql += ' AND rows.capture_day <= ?'
            range_args.append(capture_end_day)
        group_sql, group_args = self._group_sql(person_groups)
        if terms:
            try:
                cur = conn.execute(
                    'SELECT fts.rowid FROM fts JOIN rows ON rows.id = fts.rowid '
                    f'WHERE fts MATCH ?{range_sql}{group_sql} ORDER BY fts.rank LIMIT ?',
                    [fts_expression(terms), *range_args, *group_args, int(limit)],
                )
                ids.extend(r[0] for r in cur)
            except sqlite3.OperationalError:
                _LOGGER.warning('FTS query failed for terms=%s', list(terms)[:8], exc_info=True)
        if person_ids:
            marks = ','.join('?' * len(person_ids))
            cur = conn.execute(
                f'SELECT DISTINCT rows.id FROM row_people JOIN rows ON rows.id = row_people.id '
                f'WHERE row_people.person_id IN ({marks}){range_sql}{group_sql} '
                'ORDER BY rows.capture_ts DESC, rows.id ASC LIMIT ?',
                [*person_ids, *range_args, *group_args, int(limit)],
            )
            ids.extend(r[0] for r in cur)
        ids = list(dict.fromkeys(ids))
        out: List[Tuple[int, str, Dict]] = []
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ','.join('?' * len(chunk))
            for _id, filename, row_json in conn.execute(
                f'SELECT id, filename, row_json FROM rows WHERE id IN ({marks})', chunk,
            ):
                out.append((int(_id), filename, json.loads(row_json)))
        # Keep the ranked order (the IN() chunks come back in id order).
        position = {i: n for n, i in enumerate(ids)}
        out.sort(key=lambda item: position.get(item[0], 0))
        return out

    # --- gallery queries (flat memory: SQL ORDER BY / LIMIT, never a loaded list) ---

    _MIN = -1e18  # sorts photos with no known date last, like DATE_MIN

    _LIST_ORDER = {
        # Plain column order (NULLs sort smallest, so DESC puts undated photos last -- identical to the
        # old COALESCE(.., -1e18) form) lets SQLite walk the capture/upload indexes instead of
        # sorting the whole library on every page (3.7 s vs 1.2 s at a 500k offset, 124 ms vs 0 ms at 0).
        'capture': 'capture_ts DESC, filename ASC',
        'rating': 'rating DESC, likes DESC, capture_ts DESC, filename ASC',
        'likes': 'likes DESC, rating DESC, capture_ts DESC, filename ASC',
        'location': 'LOWER(filename) ASC, filename ASC',
        'name': 'LOWER(filename) ASC, filename ASC',
        'date': 'upload_ts DESC, filename ASC',  # also the default for unknown sorts
    }

    def list_page(
        self, *, sort: str = 'capture', offset: int = 0, limit: int = 24,
        capture_start_day: Optional[int] = None, capture_end_day: Optional[int] = None,
        name_contains: str = '', person_id: str = '', rating: int = 0, min_rating: int = 0, min_likes: int = 0,
    ) -> Tuple[List[str], int]:
        """(filenames for one page, total matching) in the gallery's deterministic
        order -- same semantics as ordering_utils.order_photo_entries. ``name_contains`` keeps only
        filenames containing that text (case-insensitive). ``person_id`` keeps only that person's photos
        (driven from the ``row_people`` index, so it costs the size of the person, not the library)."""
        where, args = _range_clause(capture_start_day, capture_end_day)
        source = 'rows'
        if person_id:
            source = 'rows JOIN row_people rp ON rp.id = rows.id'
            where += (' AND ' if where else ' WHERE ') + 'rp.person_id = ?'
            args = [*args, str(person_id)]
        needle = (name_contains or '').strip()
        if needle:
            escaped = needle.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            where += (' AND ' if where else ' WHERE ') + "filename LIKE ? ESCAPE '\\'"
            args = [*args, f'%{escaped}%']
        if int(rating or 0) > 0:
            where += (' AND ' if where else ' WHERE ') + 'rating = ?'
            args = [*args, int(rating)]
        elif int(min_rating or 0) > 0:
            where += (' AND ' if where else ' WHERE ') + 'rating >= ?'
            args = [*args, int(min_rating)]
        if int(min_likes or 0) > 0:
            where += (' AND ' if where else ' WHERE ') + 'likes >= ?'
            args = [*args, int(min_likes)]
        order = self._LIST_ORDER.get(sort, self._LIST_ORDER['date'])
        conn = self._conn()
        total = conn.execute(f'SELECT COUNT(*) FROM {source}{where}', args).fetchone()[0]
        names = [r[0] for r in conn.execute(
            f'SELECT filename FROM {source}{where} ORDER BY {order} LIMIT ? OFFSET ?', [*args, int(limit), max(0, int(offset))])]
        return names, int(total)

    def capture_date_position(
        self, *, date_end_ts: float, rating: int = 0, min_rating: int = 0, min_likes: int = 0,
    ) -> Tuple[int, int, str]:
        """Offset and filename of the first capture-ordered row at or before
        ``date_end_ts``. Used by the gallery date picker as a scroll target, not
        as a date filter."""
        where = ''
        args: List[object] = []
        if int(rating or 0) > 0:
            where += (' AND ' if where else ' WHERE ') + 'rating = ?'
            args.append(int(rating))
        elif int(min_rating or 0) > 0:
            where += (' AND ' if where else ' WHERE ') + 'rating >= ?'
            args.append(int(min_rating))
        if int(min_likes or 0) > 0:
            where += (' AND ' if where else ' WHERE ') + 'likes >= ?'
            args.append(int(min_likes))
        conn = self._conn()
        total = int(conn.execute(f'SELECT COUNT(*) FROM rows{where}', args).fetchone()[0])
        if total <= 0:
            return 0, 0, ''
        before_where = (where + ' AND ' if where else ' WHERE ') + 'capture_ts > ?'
        offset = int(conn.execute(f'SELECT COUNT(*) FROM rows{before_where}', [*args, float(date_end_ts)]).fetchone()[0])
        offset = max(0, min(offset, total - 1))
        row = conn.execute(
            f'SELECT filename FROM rows{where} ORDER BY capture_ts DESC, filename ASC LIMIT 1 OFFSET ?',
            [*args, offset],
        ).fetchone()
        return offset, total, str(row[0] if row else '')

    def all_filenames(self) -> set:
        """Every filename in the library database (used to reconcile against the metadata table)."""
        return {r[0] for r in self._conn().execute('SELECT filename FROM rows')}

    def existing_filenames(self, filenames: Sequence[str]) -> set:
        """The subset of ``filenames`` that are in the library (one indexed SQL lookup per 500 names)."""
        names = list(dict.fromkeys(str(n) for n in filenames))
        found: set = set()
        conn = self._conn()
        for start in range(0, len(names), 500):
            chunk = names[start:start + 500]
            marks = ','.join('?' * len(chunk))
            found.update(r[0] for r in conn.execute(f'SELECT filename FROM rows WHERE filename IN ({marks})', chunk))
        return found

    def filter_page(
        self, *, min_rating: int = 0, min_likes: int = 0, offset: int = 0, limit: int = 24,
        capture_start_day: Optional[int] = None, capture_end_day: Optional[int] = None,
        latitude: Optional[float] = None, longitude: Optional[float] = None, radius_degrees: float = 0.0,
    ) -> Tuple[List[str], int]:
        """Rating/likes/date/location filter, ordered rating -> likes -> recency
        -> filename (what /photos/filter always returned). With a location
        filter active, photos without coordinates are excluded (the old code
        compared them as latitude/longitude 0, so they only matched by accident)."""
        where, args = _range_clause(capture_start_day, capture_end_day)
        clauses = ['rating >= ?', 'likes >= ?']
        clause_args: List[object] = [int(min_rating), int(min_likes)]
        if latitude is not None and longitude is not None:
            clauses.append('(lat IS NOT NULL AND lon IS NOT NULL AND ((lat - ?) * (lat - ?) + (lon - ?) * (lon - ?)) <= ?)')
            clause_args += [latitude, latitude, longitude, longitude, radius_degrees * radius_degrees]
        sql_where = (where + ' AND ' if where else ' WHERE ') + ' AND '.join(clauses)
        all_args = [*args, *clause_args]
        conn = self._conn()
        total = conn.execute(f'SELECT COUNT(*) FROM rows{sql_where}', all_args).fetchone()[0]
        names = [r[0] for r in conn.execute(
            f'SELECT filename FROM rows{sql_where} ORDER BY rating DESC, likes DESC, '
            'upload_ts DESC, filename ASC LIMIT ? OFFSET ?',
            [*all_args, int(limit), max(0, int(offset))])]
        return names, int(total)

    def on_this_day(self, month: int, day: int, exclude_year: int) -> Dict[int, int]:
        """{year: photo count} for photos captured on this month/day in other years."""
        return {int(y): int(c) for y, c in self._conn().execute(
            'SELECT capture_year, COUNT(*) FROM rows WHERE capture_md = ? AND capture_year != ? GROUP BY capture_year',
            (month * 100 + day, exclude_year))}

    def top_rated(self, filenames: Sequence[str], limit: int = 12) -> List[str]:
        """The best `limit` of these filenames by rating -> likes -> recency ->
        filename (album-cover order). Chunked IN() queries keep the SQL variable
        count small and memory O(limit) however large the album is."""
        best: List[Tuple] = []
        conn = self._conn()
        names = list(dict.fromkeys(filenames))
        for start in range(0, len(names), 500):
            chunk = names[start:start + 500]
            marks = ','.join('?' * len(chunk))
            for filename, rating, likes, upload in conn.execute(
                f'SELECT filename, rating, likes, COALESCE(upload_ts, {self._MIN}) FROM rows WHERE filename IN ({marks})', chunk,
            ):
                best.append((-rating, -likes, -upload, filename))
            best.sort()
            del best[limit:]
        return [filename for *_, filename in best]

    def row_count(self) -> int:
        return int(self._conn().execute('SELECT COUNT(*) FROM rows').fetchone()[0])

    def iter_smart_rows(self):
        """Stream every photo as a metadata-shaped row for the smart-album rules
        (location / upload day / capture day / person / tag), read from local disk
        instead of scanning the table. Dates come from the stored timestamps
        (``_capture_dt`` / ``_upload_dt``); tags are the high-confidence set the
        search index keeps."""
        cur = self._conn().execute('SELECT filename, row_json, capture_ts, upload_ts, lat, lon FROM rows')
        for filename, row_json, capture_ts, upload_ts, lat, lon in cur:
            try:
                row = json.loads(row_json)
            except Exception:
                continue
            row['RowKey'] = filename
            if lat is not None and lon is not None:
                row['latitude'], row['longitude'] = repr(lat), repr(lon)
            else:
                row.pop('latitude', None)
                row.pop('longitude', None)
            tags: List[str] = []
            for field in ('subjectTags', 'tags'):
                try:
                    tags.extend(str(t) for t in json.loads(row.get(field) or '[]'))
                except Exception:
                    pass
            row['tags'] = json.dumps(tags, ensure_ascii=False)
            row['objects'] = '[]'
            if capture_ts is not None:
                row['_capture_dt'] = datetime.fromtimestamp(capture_ts, tz=timezone.utc)
            if upload_ts is not None:
                row['_upload_dt'] = datetime.fromtimestamp(upload_ts, tz=timezone.utc)
            yield row


# --- blob storage + local ephemeral cache -------------------------------------

def _blob_names(user_id: str) -> Tuple[str, str]:
    import storage_utils as su
    key = su._vector_index_blob_key(user_id)
    return f'{key}-searchdb.sqlite.gz', f'{key}-searchdb.json'


def _delta_blob_name(user_id: str, seq: int) -> str:
    import storage_utils as su
    return f'{su._vector_index_blob_key(user_id)}-searchdelta-{int(seq):06d}.json.gz'


def _blob_client(name: str):
    import storage_utils as su
    return su._get_blob_client(su._lexical_index_container_name(), name)


MANIFEST_TTL_SECONDS = float(os.getenv('SEARCH_DB_MANIFEST_TTL_SECONDS', '5'))
_MANIFEST_CACHE: Dict[str, Tuple[float, Dict]] = {}


def invalidate_manifest_cache(user_id: Optional[str] = None) -> None:
    with _LOCKS_GUARD:
        if user_id is None:
            _MANIFEST_CACHE.clear()
        else:
            _MANIFEST_CACHE.pop(user_id, None)


def load_manifest(user_id: str) -> Dict:
    """The library's manifest. Every search/list/filter request asks for it, so a successful read
    is reused for a few seconds (a storage round trip per request otherwise); failures are not
    cached."""
    if MANIFEST_TTL_SECONDS > 0:
        with _LOCKS_GUARD:
            hit = _MANIFEST_CACHE.get(user_id)
        if hit is not None and time.monotonic() - hit[0] < MANIFEST_TTL_SECONDS:
            return hit[1]
    _, manifest_name = _blob_names(user_id)
    client = _blob_client(manifest_name)
    if client is None:
        return {}
    try:
        parsed = json.loads(client.download_blob().readall().decode('utf-8'))
    except Exception:
        return {}
    manifest = parsed if isinstance(parsed, dict) else {}
    if MANIFEST_TTL_SECONDS > 0 and manifest:
        with _LOCKS_GUARD:
            _MANIFEST_CACHE[user_id] = (time.monotonic(), manifest)
    return manifest


def is_building(user_id: str) -> bool:
    """True while the library's first build is still indexing (results so far are partial)."""
    return isinstance(load_manifest(user_id).get('building'), dict)


def needs_build(user_id: str) -> bool:
    """True only when the library genuinely has no usable database: no manifest (never built) or
    one on an older schema. A transient storage error, or a current manifest whose database file
    merely failed to download on this replica, is NOT a reason to rebuild the library -- that
    would turn a blip into a multi-minute full scan."""
    _, manifest_name = _blob_names(user_id)
    client = _blob_client(manifest_name)
    if client is None:
        return False
    try:
        parsed = json.loads(client.download_blob().readall().decode('utf-8'))
    except Exception as exc:
        return type(exc).__name__ == 'ResourceNotFoundError'
    if not isinstance(parsed, dict) or not parsed.get('sourceVersion'):
        return True
    return parsed.get('schemaVersion') != SCHEMA_VERSION


def is_current(user_id: str, lexical_source_version: str) -> bool:
    manifest = load_manifest(user_id)
    return bool(
        manifest.get('sourceVersion')
        and _lineage(manifest) == lexical_source_version      # compaction changes the file version, not the lineage
        and manifest.get('schemaVersion') == SCHEMA_VERSION
    )


class SearchDbSink:
    """Stream-build sink: add(row) per photo, finish() builds, gzips and uploads
    the database + manifest (publishing only on success)."""

    def __init__(self, user_id: str, source_version: str, workdir: str, updated_at: Optional[str] = None) -> None:
        self.user_id = user_id
        self.source_version = source_version
        self.updated_at = updated_at or source_version
        self.db_path = os.path.join(workdir, 'search.sqlite')
        self._builder = DatabaseBuilder(self.db_path)

    def add(self, row: Dict) -> None:
        self._builder.add(row)

    def abort(self) -> None:
        self._builder.abort()

    def finish(self) -> None:
        user_id = self.user_id
        data_name, manifest_name = _blob_names(user_id)
        data_client, manifest_client = _blob_client(data_name), _blob_client(manifest_name)
        previous_seq = int(load_manifest(user_id).get('deltaSeq') or 0)
        with perf_instrumentation.span('searchdb.build', user=user_id, rows=self._builder.count):
            count = self._builder.finish()
        if data_client is None or manifest_client is None:
            raise RuntimeError('search database blob storage is not configured')
        gz_path = self.db_path + '.gz'
        with perf_instrumentation.span('searchdb.gzip', user=user_id):
            with open(self.db_path, 'rb') as src, gzip.open(gz_path, 'wb', compresslevel=5) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        perf_instrumentation.log_event(
            'searchdb_built', user=user_id, rows=count,
            sqlite_mb=round(os.path.getsize(self.db_path) / 1048576, 1), gz_mb=round(os.path.getsize(gz_path) / 1048576, 1),
        )
        from azure.storage.blob import ContentSettings
        with perf_instrumentation.span('searchdb.upload', user=user_id):
            with open(gz_path, 'rb') as fh:
                data_client.upload_blob(fh, overwrite=True, content_settings=ContentSettings(content_type='application/gzip'))
        manifest_client.upload_blob(
            json.dumps({
                'userId': user_id, 'sourceVersion': self.source_version, 'schemaVersion': SCHEMA_VERSION,
                'rowCount': count, 'updatedAt': self.updated_at, 'deltaSeq': 0, 'deltaRows': 0,
                'lineage': self.source_version, 'baseSeq': 0,
            }, separators=(',', ':')).encode('utf-8'),
            overwrite=True, content_settings=ContentSettings(content_type='application/json'),
        )
        invalidate_manifest_cache(user_id)
        # A new base absorbs every earlier delta; their blobs are garbage now.
        for seq in range(1, previous_seq + 1):
            client = _blob_client(_delta_blob_name(user_id, seq))
            if client is not None:
                try:
                    client.delete_blob()
                except Exception:
                    pass


# --- incremental updates ----------------------------------------------------------

# Rebuild (compact) instead of appending another delta when the log gets long or large.
DELTA_MAX_COUNT = int(os.getenv('SEARCH_DB_DELTA_MAX_COUNT', '200'))
DELTA_MAX_ROW_FRACTION = float(os.getenv('SEARCH_DB_DELTA_MAX_ROW_FRACTION', '0.25'))
# Floor for the fraction rule: against an empty or tiny base it allowed no delta at all, so a new library's
# photos could never be added (every attempt asked for a rebuild that compacted the same empty base).
DELTA_MIN_ROW_BUDGET = int(os.getenv('SEARCH_DB_DELTA_MIN_ROW_BUDGET', '5000'))


def delta_budget_exceeded(manifest: Dict, adding: int) -> bool:
    base_rows = max(1, int(manifest.get('rowCount') or 0))
    return (
        int(manifest.get('deltaSeq') or 0) >= DELTA_MAX_COUNT
        or (int(manifest.get('deltaRows') or 0) + adding) > max(DELTA_MIN_ROW_BUDGET, DELTA_MAX_ROW_FRACTION * base_rows)
    )


def available_delta_row_capacity(manifest: Dict, max_rows_per_delta: int) -> int:
    """Rows that may be appended before the delta log must be compacted.

    The caller can consume part of a large dirty set up to this limit, compact
    that progress into the base, then continue. This keeps the existing
    count/fraction bounds without making an oversized import an all-or-nothing
    operation.
    """
    remaining_deltas = DELTA_MAX_COUNT - int(manifest.get('deltaSeq') or 0)
    if remaining_deltas <= 0:
        return 0
    base_rows = max(1, int(manifest.get('rowCount') or 0))
    row_limit = int(max(DELTA_MIN_ROW_BUDGET, DELTA_MAX_ROW_FRACTION * base_rows))
    remaining_rows = row_limit - int(manifest.get('deltaRows') or 0)
    if remaining_rows <= 0:
        return 0
    return min(remaining_rows, remaining_deltas * max(1, int(max_rows_per_delta)))


def _lineage(manifest: Dict) -> str:
    return str(manifest.get('lineage') or manifest.get('sourceVersion') or '')


def publish_delta(
    user_id: str, upserts: List[Dict], deletes: List[str], *, building: Optional[Dict] = None,
) -> Optional[int]:
    """Append one delta to the library's database. Returns the new sequence number, or None if
    nothing could be published (no base, base replaced meanwhile, storage error).

    The delta blob is written first, then the manifest is advanced with a compare-and-swap on its
    ETag, so a concurrent rebuild or another publisher can never be overwritten: the loser simply
    discards its delta. ``building`` (a resumable first build) is written in the SAME manifest
    update, so "this chunk is published" and "the cursor moved past it" are one atomic fact."""
    manifest = load_manifest(user_id)
    base_version = str(manifest.get('sourceVersion') or '')
    if not base_version or manifest.get('schemaVersion') != SCHEMA_VERSION:
        return None
    lineage = _lineage(manifest)
    seq = int(manifest.get('deltaSeq') or 0) + 1
    delta_client = _blob_client(_delta_blob_name(user_id, seq))
    _, manifest_name = _blob_names(user_id)
    manifest_client = _blob_client(manifest_name)
    if delta_client is None or manifest_client is None:
        return None
    from azure.storage.blob import ContentSettings
    payload = gzip.compress(json.dumps(
        {'lineage': lineage, 'baseVersion': base_version, 'seq': seq, 'upserts': upserts, 'deletes': deletes},
        ensure_ascii=False, separators=(',', ':')).encode('utf-8'), compresslevel=5)
    try:
        with perf_instrumentation.span('searchdb.delta.upload', user=user_id, upserts=len(upserts), deletes=len(deletes)):
            delta_client.upload_blob(payload, overwrite=True, content_settings=ContentSettings(content_type='application/gzip'))
        updated = _cas_update_manifest(
            user_id, manifest_client,
            lambda current: (
                None if _lineage(current) != lineage or int(current.get('deltaSeq') or 0) != seq - 1 else {
                    **current, 'deltaSeq': seq,
                    'deltaRows': int(current.get('deltaRows') or 0) + len(upserts) + len(deletes),
                    'updatedAt': datetime.now(timezone.utc).isoformat(),
                    **({'building': building} if building is not None else {}),
                }),
        )
    except Exception:
        _LOGGER.warning('Publishing search DB delta failed for %s', user_id, exc_info=True)
        return None
    if updated is None:
        _LOGGER.info('Search DB delta %s discarded: manifest moved on', seq)
        return None
    perf_instrumentation.log_event('searchdb_delta_published', user=user_id, seq=seq, upserts=len(upserts), deletes=len(deletes))
    return seq


def _cas_update_manifest(user_id: str, manifest_client, mutate, attempts: int = 6) -> Optional[Dict]:
    """Read-modify-write the manifest with an ETag condition. ``mutate(current)`` returns the new
    manifest, or None to give up (the world changed in a way that makes this update wrong). A lost
    ETag race just re-reads and re-evaluates, up to ``attempts`` times."""
    from azure.storage.blob import ContentSettings
    for _ in range(attempts):
        invalidate_manifest_cache(user_id)
        try:
            props = manifest_client.get_blob_properties()
        except Exception:
            props = None
        current = json.loads(manifest_client.download_blob().readall().decode('utf-8'))
        updated = mutate(current)
        if updated is None:
            return None
        kwargs = {}
        etag = getattr(props, 'etag', None)
        if etag:
            from azure.core import MatchConditions
            kwargs = {'etag': etag, 'match_condition': MatchConditions.IfNotModified}
        try:
            manifest_client.upload_blob(
                json.dumps(updated, separators=(',', ':')).encode('utf-8'), overwrite=True,
                content_settings=ContentSettings(content_type='application/json'), **kwargs)
        except Exception:
            if not etag:
                raise
            continue  # lost the race: re-read and decide again
        invalidate_manifest_cache(user_id)
        return updated
    return None


def sync_deltas(user_id: str, db: 'SearchDatabase', manifest: Dict) -> None:
    """Bring this replica's local copy up to the manifest's delta sequence. Best-effort: if a delta
    cannot be fetched or applied the replica keeps serving what it has (slightly stale) and retries
    on a later request."""
    target = int(manifest.get('deltaSeq') or 0)
    if db.applied_seq() >= target:
        return
    with _download_lock(user_id):
        db._seq = None          # another process may have applied deltas since this one last looked
        while db.applied_seq() < target:
            seq = db.applied_seq() + 1
            client = _blob_client(_delta_blob_name(user_id, seq))
            if client is None:
                return
            try:
                with perf_instrumentation.span('searchdb.delta.apply', user=user_id, seq=seq):
                    delta = json.loads(gzip.decompress(client.download_blob().readall()).decode('utf-8'))
                    delta_lineage = str(delta.get('lineage') or delta.get('baseVersion') or '')
                    if delta_lineage != _lineage(manifest) or int(delta.get('seq') or 0) != seq:
                        return  # belongs to another lineage: a rebuild is publishing; next request re-resolves
                    db.apply_delta(delta)
            except Exception:
                _LOGGER.warning('Applying search DB delta %s failed for %s', seq, user_id, exc_info=True)
                return


def _build_dir() -> str:
    """Scratch space for building/compacting a database (local disk: SQLite needs real locking)."""
    base = os.getenv('INDEX_BUILD_SQLITE_DIR', '').strip() or os.path.join(tempfile.gettempdir(), 'photostore-index-build')
    os.makedirs(base, exist_ok=True)
    return base


def _require_disk(path: str, needed_bytes: int, what: str) -> None:
    free = shutil.disk_usage(path).free
    if free < needed_bytes:
        raise RuntimeError(
            f'Not enough free disk to {what}: need ~{needed_bytes // 1048576} MB, have {free // 1048576} MB in {path}')


def _upload_database_file(user_id: str, db_path: str, label: str) -> None:
    data_name, _ = _blob_names(user_id)
    data_client = _blob_client(data_name)
    if data_client is None:
        raise RuntimeError('search database blob storage is not configured')
    gz_path = db_path + '.gz'
    with perf_instrumentation.span(f'searchdb.{label}.gzip', user=user_id):
        with open(db_path, 'rb') as src, gzip.open(gz_path, 'wb', compresslevel=5) as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
    from azure.storage.blob import ContentSettings
    try:
        with perf_instrumentation.span(f'searchdb.{label}.upload', user=user_id, mb=round(os.path.getsize(gz_path) / 1048576, 1)):
            with open(gz_path, 'rb') as fh:
                data_client.upload_blob(fh, overwrite=True, content_settings=ContentSettings(content_type='application/gzip'))
    finally:
        try:
            os.remove(gz_path)
        except OSError:
            pass


def create_empty_base(user_id: str, building: Dict) -> str:
    """Publish an EMPTY database + manifest for a library that has none, so a resumable first build
    can append deltas to it and search works (on what has been indexed so far) from the first chunk.
    Returns the new base version."""
    version = datetime.now(timezone.utc).isoformat()
    workdir = tempfile.mkdtemp(prefix='searchdb-empty-', dir=_build_dir())
    try:
        path = os.path.join(workdir, 'search.sqlite')
        DatabaseBuilder(path).finish()
        _upload_database_file(user_id, path, 'empty')
        _, manifest_name = _blob_names(user_id)
        manifest_client = _blob_client(manifest_name)
        from azure.storage.blob import ContentSettings
        manifest_client.upload_blob(
            json.dumps({
                'userId': user_id, 'sourceVersion': version, 'schemaVersion': SCHEMA_VERSION, 'rowCount': 0,
                'updatedAt': version, 'deltaSeq': 0, 'deltaRows': 0, 'lineage': version, 'baseSeq': 0,
                'building': building,
            }, separators=(',', ':')).encode('utf-8'),
            overwrite=True, content_settings=ContentSettings(content_type='application/json'))
        invalidate_manifest_cache(user_id)
        return version
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def compact_database(user_id: str) -> Optional[Dict]:
    """Fold the delta log into a fresh base file -- WITHOUT touching the table.

    The worker's own synced local copy already equals base + every delta, so compaction is: snapshot
    that file (SQLite backup), merge the FTS segments, upload it as the new base, and advance the
    manifest (compare-and-swap) so replicas download one file instead of replaying the log. Deltas
    published while this runs keep their sequence numbers and simply sit on top of the new base.
    Cost follows library size on LOCAL DISK, not table scans. Returns the new manifest, or None."""
    manifest = load_manifest(user_id)
    if not manifest.get('sourceVersion') or manifest.get('schemaVersion') != SCHEMA_VERSION:
        return None
    db = open_database(user_id)
    if db is None:
        return None
    applied = db.applied_seq()
    lineage = _lineage(manifest)
    workdir = tempfile.mkdtemp(prefix='searchdb-compact-', dir=_build_dir())
    try:
        _require_disk(workdir, int(os.path.getsize(db.path) * 2.5) + (64 << 20), 'compact the search database')
        dst_path = os.path.join(workdir, 'search.sqlite')
        with perf_instrumentation.step('searchdb.compact.snapshot', user=user_id, delta_seq=applied):
            src = sqlite3.connect(f'file:{db.path}?mode=ro', uri=True)
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
                dst.execute('PRAGMA journal_mode=DELETE')
                dst.execute("INSERT INTO fts(fts) VALUES('optimize')")
                count = int(dst.execute('SELECT COUNT(*) FROM rows').fetchone()[0])
                dst.execute("INSERT OR REPLACE INTO meta VALUES('delta_seq', ?)", (str(applied),))
                dst.commit()
                dst.execute('VACUUM')
            finally:
                dst.close()
                src.close()
        _upload_database_file(user_id, dst_path, 'compact')
        _, manifest_name = _blob_names(user_id)
        manifest_client = _blob_client(manifest_name)
        new_version = datetime.now(timezone.utc).isoformat()

        def _mutate(current: Dict) -> Optional[Dict]:
            current_delta = int(current.get('deltaSeq') or 0)
            if _lineage(current) != lineage or not (int(current.get('baseSeq') or 0) <= applied <= current_delta):
                return None
            remaining = current_delta - applied
            rows_in_log = int(current.get('deltaRows') or 0)
            return {**current, 'sourceVersion': new_version, 'rowCount': count, 'baseSeq': applied,
                    'deltaRows': int(rows_in_log * remaining / max(1, current_delta)),
                    'updatedAt': new_version}

        updated = _cas_update_manifest(user_id, manifest_client, _mutate)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if updated is None:
        _LOGGER.info('Search DB compaction for %s discarded: manifest moved on', user_id)
        return None
    # Everything up to `applied` is inside the new base now.
    for seq in range(1, applied + 1):
        client = _blob_client(_delta_blob_name(user_id, seq))
        if client is not None:
            try:
                client.delete_blob()
            except Exception:
                pass
    perf_instrumentation.log_event('searchdb_compacted', user=user_id, rows=count, folded_deltas=applied)
    return updated


def write_for_snapshot(user_id: str, snapshot) -> bool:
    """Build + upload the database from an in-memory snapshot (legacy/tests; the
    index build streams rows through SearchDbSink instead). Best-effort."""
    workdir = tempfile.mkdtemp(prefix='searchdb-build-')
    try:
        sink = SearchDbSink(user_id, snapshot.source_version, workdir, snapshot.updated_at)
        try:
            for row in snapshot.rows:
                sink.add(row)
            sink.finish()
        except Exception:
            sink.abort()
            raise
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


_DB_FILE = re.compile(r'^(?P<prefix>.+-[0-9a-f]{12})\.sqlite$')


def _remove_database_files(path: str) -> None:
    """Delete one finished database and its SQLite sidecars."""
    for suffix in ('', '-wal', '-shm', '-journal'):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _evict_user(user_id: str, keep: Optional[str] = None) -> None:
    """Remove this user's OLDER database files. Only finished ``<user>-<digest>.sqlite`` files (and
    their sidecars) are touched: another process's in-progress download, or the sidecars of the
    database being kept, must never be deleted from under it."""
    prefix = f'{_safe(user_id)}-'
    try:
        for name in os.listdir(SEARCH_DB_DIR):
            full = os.path.join(SEARCH_DB_DIR, name)
            if name.startswith(prefix) and _DB_FILE.match(name) and full != keep:
                _remove_database_files(full)
    except OSError:
        pass


def _enforce_budget(keep: str) -> None:
    try:
        files = [os.path.join(SEARCH_DB_DIR, n) for n in os.listdir(SEARCH_DB_DIR) if _DB_FILE.match(n)]
        files.sort(key=lambda p: os.path.getmtime(p))
        total = sum(os.path.getsize(p) for p in files)
        for path in files:
            if total <= MAX_CACHE_BYTES:
                break
            if path != keep:
                total -= os.path.getsize(path)
                _remove_database_files(path)
    except OSError:
        pass


_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.Lock] = {}
_OPEN: Dict[str, SearchDatabase] = {}


def _user_lock(user_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(user_id, threading.Lock())


@contextlib.contextmanager
def _download_lock(user_id: str):
    """Serialises a download across threads AND worker processes. The server runs several processes
    (and briefly overlaps a recycled worker with its replacement) that share this disk, so a thread
    lock alone let two processes unpack and swap the same database at once."""
    with _user_lock(user_id):
        os.makedirs(SEARCH_DB_DIR, exist_ok=True)
        handle = open(os.path.join(SEARCH_DB_DIR, f'.{_safe(user_id)}.lock'), 'a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()


def _is_usable(path: str) -> bool:
    """True when ``path`` is a real library database (has its tables), not an empty or partial file."""
    try:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            conn.execute("SELECT value FROM meta WHERE key='delta_seq'").fetchone()
            conn.execute('SELECT 1 FROM rows LIMIT 1').fetchone()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


_VERIFIED: set = set()


def open_database(user_id: str, *, allow_download: bool = True) -> Optional[SearchDatabase]:
    """The library's current search DB on local ephemeral disk, downloading and
    unpacking it (streamed, never fully in memory) if this replica lacks the
    current version. None if no current DB exists yet (or this replica's copy was unusable and has
    been discarded: the next request downloads a fresh one)."""
    manifest = load_manifest(user_id)
    version = str(manifest.get('sourceVersion') or '')
    if not version or manifest.get('schemaVersion') != SCHEMA_VERSION:
        return None
    path = _local_path(user_id, version)
    with _LOCKS_GUARD:
        existing = _OPEN.get(path)
    if existing is not None and os.path.exists(path):
        if allow_download:
            try:
                sync_deltas(user_id, existing, manifest)
            except sqlite3.DatabaseError:
                _discard(path)
                return None
        return existing
    if not os.path.exists(path):
        if not allow_download:
            return None
        with _download_lock(user_id):
            if not os.path.exists(path):
                if not _download(user_id, path):
                    return None
    if path not in _VERIFIED:
        if not _is_usable(path):
            _LOGGER.error('Local search DB %s is unusable; discarding it', path)
            _discard(path)
            return None
        _VERIFIED.add(path)
    db = SearchDatabase(path)
    with _LOCKS_GUARD:
        _OPEN[path] = db
        for stale in [p for p in _OPEN if p != path and os.path.basename(p).startswith(f'{_safe(user_id)}-')]:
            _OPEN.pop(stale, None)
    _evict_user(user_id, keep=path)
    if allow_download:
        try:
            sync_deltas(user_id, db, manifest)
        except sqlite3.DatabaseError:
            _discard(path)
            return None
    return db


def report_failure(db: Optional['SearchDatabase'], exc: BaseException) -> None:
    """Call when a query on ``db`` raised. A database-level error (missing tables, corruption) means this
    replica's local copy is bad: drop it so the next request downloads a fresh one instead of failing forever."""
    if db is not None and isinstance(exc, sqlite3.DatabaseError):
        _LOGGER.error('Discarding local search DB after %r', exc)
        _discard(db.path)


def _discard(path: str) -> None:
    with _LOCKS_GUARD:
        _OPEN.pop(path, None)
    _VERIFIED.discard(path)
    _remove_database_files(path)


def _download(user_id: str, path: str) -> bool:
    """Fetch and unpack the base database to ``path`` (caller holds the download lock). Work happens in
    a private scratch directory so no other process's cleanup can touch it, and the file is only moved
    into place once it has been checked to be a real database."""
    data_name, _ = _blob_names(user_id)
    client = _blob_client(data_name)
    if client is None:
        return False
    os.makedirs(SEARCH_DB_DIR, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix='dl-', dir=SEARCH_DB_DIR)
    tmp_gz = os.path.join(scratch, 'db.gz')
    tmp_db = os.path.join(scratch, 'db.sqlite')
    try:
        with perf_instrumentation.span('searchdb.download', user=user_id):
            with open(tmp_gz, 'wb') as fh:
                client.download_blob().readinto(fh)
        with perf_instrumentation.span('searchdb.unpack', user=user_id):
            with gzip.open(tmp_gz, 'rb') as src, open(tmp_db, 'wb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        if not _is_usable(tmp_db):
            raise RuntimeError('downloaded search database has no tables')
        # WAL lets delta updates be applied in place while other threads keep reading.
        conn = sqlite3.connect(f'file:{tmp_db}?mode=rw', uri=True)
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        finally:
            conn.close()
        os.replace(tmp_db, path)
        _enforce_budget(keep=path)
        perf_instrumentation.log_event('searchdb_ready', user=user_id, mb=round(os.path.getsize(path) / 1048576, 1))
        return True
    except Exception:
        _LOGGER.exception('Search DB download failed for user %s', user_id)
        return False
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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
