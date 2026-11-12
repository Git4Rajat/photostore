"""Exact disk-backed summary contributions for two explicit row shapes.

SummaryContributionStore preserves the raw full streaming builders unchanged.
DatabaseSummaryAdapter maintains reduced Explore memberships and stored UTC
day counts INSIDE search SQLite, under its builder/delta transaction. It does
not use the raw streaming timeline algorithm for database summaries. Neither
adapter owns source scans, source wrappers, or remote publication.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Optional

from ordering_utils import metadata_capture_datetime
from search_utils import parse_json_list, parse_tags
from timeline_metadata import _cover_score


FORMAT = 'streaming-summary-v1'
DATABASE_FORMAT = 'database-reduced-summary-v1'


class SummarySchemaMissing(RuntimeError):
    """An explicit worker compaction is required; never bootstrap on a read."""


def smart_row_from_record(record: dict) -> Optional[dict]:
    """Exactly the reduced projection used by SearchDatabase.iter_smart_rows.

    The database's filename replaces the raw RowKey; coordinates are restored,
    subject tags precede other tags, and raw objects are deliberately absent.
    Kept here so full streaming seeds and delta replacements share that shape.
    """
    try:
        row = json.loads(record['row_json'])
    except Exception:
        return None
    row['RowKey'] = record['filename']
    lat, lon = record.get('lat'), record.get('lon')
    # SQLite REAL affinity converts integer bindings to floats and NaN to
    # NULL. Seed from those stored values, not the raw Python binding shape.
    lat = float(lat) if lat is not None else None
    lon = float(lon) if lon is not None else None
    lat = None if lat is not None and math.isnan(lat) else lat
    lon = None if lon is not None and math.isnan(lon) else lon
    if lat is not None and lon is not None:
        row['latitude'], row['longitude'] = repr(lat), repr(lon)
    else:
        row.pop('latitude', None)
        row.pop('longitude', None)
    tags = []
    for field in ('subjectTags', 'tags'):
        try:
            tags.extend(str(t) for t in json.loads(row.get(field) or '[]'))
        except Exception:
            pass
    row['tags'] = json.dumps(tags, ensure_ascii=False)
    row['objects'] = '[]'
    for field, target in (('capture_ts', '_capture_dt'), ('upload_ts', '_upload_dt')):
        if record.get(field) is not None:
            row[target] = datetime.fromtimestamp(record[field], tz=timezone.utc)
    return row


class DatabaseSummaryAdapter:
    """Aggregate tables in the SEARCH file, using the caller's transaction.

    No connection, commit, source scan, sidecar, or publication is owned here.
    Row ids are the full iter_smart_rows order (SQLite INTEGER PRIMARY KEY).
    Only touched group candidates are repaired after insertion/removal.
    Timeline counts use the stored capture_day (the capture's own clock date, as
    the streaming timeline buckets it), NOT the streaming timeline's CRC cover
    semantics. Covers remain SearchDatabase's SQL queries.
    """

    def __init__(self, conn):
        self.conn = conn

    def ready(self):
        row = self.conn.execute("SELECT value FROM meta WHERE key='summary_schema'").fetchone()
        return bool(row and row[0] == DATABASE_FORMAT)

    def create(self):
        # execute, not executescript: DDL must not implicitly commit delta/backup work.
        for sql in (
            'CREATE TABLE summary_days(day INTEGER PRIMARY KEY, count INTEGER NOT NULL)',
            'CREATE TABLE summary_members(id INTEGER NOT NULL, occurrence INTEGER NOT NULL, '
            'kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(id,occurrence))',
            'CREATE INDEX summary_candidates ON summary_members(kind,key,id,occurrence)',
            'CREATE TABLE summary_groups(kind TEXT NOT NULL, key TEXT NOT NULL, count INTEGER NOT NULL, '
            'first_id INTEGER NOT NULL, first_occurrence INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(kind,key))',
            'CREATE INDEX summary_top ON summary_groups(kind,count DESC,first_id,first_occurrence)',
        ):
            self.conn.execute(sql)
        self.conn.execute("INSERT INTO meta VALUES('summary_schema', ?)", (DATABASE_FORMAT,))

    def change(self, row_id: int, record: dict, *, remove=False):
        conn = self.conn
        day = record.get('capture_day') or 0
        sign = -1 if remove else 1
        conn.execute('INSERT INTO summary_days VALUES(?,?) ON CONFLICT(day) DO UPDATE SET count=count+excluded.count',
                     (day, sign))
        conn.execute('DELETE FROM summary_days WHERE day=? AND count=0', (day,))
        if remove:
            memberships = list(conn.execute('SELECT kind,key FROM summary_members WHERE id=?', (row_id,)))
            conn.execute('DELETE FROM summary_members WHERE id=?', (row_id,))
        else:
            row = smart_row_from_record(record)
            memberships = []
            for occurrence, member in enumerate(explore_memberships(row) if row is not None else ()):
                payload = {'label': member.label, 'filename': record['filename']}
                if member.kind == 'places':
                    payload.update(latitude=member.latitude, longitude=member.longitude)
                conn.execute('INSERT INTO summary_members VALUES(?,?,?,?,?)',
                             (row_id, occurrence, member.kind, member.key, json.dumps(payload, ensure_ascii=False)))
                memberships.append((member.kind, member.key))
        for kind, key in memberships:
            conn.execute('INSERT INTO summary_groups VALUES(?,?,?,0,0,?) '
                         'ON CONFLICT(kind,key) DO UPDATE SET count=count+excluded.count', (kind, key, sign, '{}'))
        for kind, key in set(memberships):
            conn.execute('DELETE FROM summary_groups WHERE kind=? AND key=? AND count=0', (kind, key))
            first = conn.execute('SELECT id,occurrence,payload FROM summary_members WHERE kind=? AND key=? '
                                 'ORDER BY id,occurrence LIMIT 1', (kind, key)).fetchone()
            if first:
                conn.execute('UPDATE summary_groups SET first_id=?,first_occurrence=?,payload=? WHERE kind=? AND key=?',
                             (*first, kind, key))

    def groups(self, limit=24):
        if not self.ready():
            raise SummarySchemaMissing('summary schema missing: compact the search database explicitly')
        return {kind: [dict(json.loads(payload), count=count) for count, payload in self.conn.execute(
            'SELECT count,payload FROM summary_groups WHERE kind=? '
            'ORDER BY count DESC,first_id,first_occurrence LIMIT ?', (kind, limit))]
            for kind in ('places', 'things')}


@dataclass(frozen=True)
class Membership:
    kind: str
    key: str
    label: str
    latitude: str = ''
    longitude: str = ''


@dataclass(frozen=True)
class Contribution:
    """One stream occurrence; identity need not be a nonempty filename.

    position is the caller's stable, unique order in the reference full stream.
    Repeated normalized terms remain repeated memberships: Explore counts them.
    """

    identity: str
    position: int
    filename: str
    memberships: tuple[Membership, ...]
    capture_day: Optional[int]
    timeline_filename: str
    liked_order: int
    cover_rank: int


def explore_memberships(row: dict) -> tuple[Membership, ...]:
    """Exact ExploreAccumulator.add normalization on the supplied row shape."""
    filename = row.get('RowKey') or ''
    if not isinstance(filename, str):
        raise ValueError('RowKey must be a string or absent')

    def normalize(value):
        folded = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode('ascii')
        return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9]+', ' ', folded.lower())).strip()

    def title(value):
        cleaned = re.sub(r'\s+', ' ', str(value or '').replace('_', ' ')).strip()
        return cleaned.title() if cleaned.islower() else cleaned

    memberships = []
    if filename:
        city = str(row.get('locationCity') or '').strip()
        country = str(row.get('locationCountry') or '').strip()
        address = str(row.get('address') or '').strip()
        lat = str(row.get('latitude') or '').strip()
        lon = str(row.get('longitude') or '').strip()
        label = ', '.join(part for part in (city, country) if part) or address
        if not label and lat and lon:
            label = f'{lat[:8]}, {lon[:8]}'
        key = normalize(label)
        if key:
            memberships.append(Membership('places', key, title(label), lat, lon))
        terms = parse_tags(row.get('tags', '[]')) + parse_json_list(row.get('objects', '[]'))
        for term in dict.fromkeys(terms):
            key = normalize(term)
            if key:
                memberships.append(Membership('things', key, title(term)))
    return tuple(memberships)


def contribution_from_row(row: dict, *, identity: str, position: int) -> Contribution:
    """Mirror both raw full streaming accumulators on the SAME admitted row.

    The database adapter explicitly supplies the reduced smart-row shape to
    explore_memberships and never uses this raw timeline's date/cover rules.
    """
    filename = row.get('RowKey') or ''
    memberships = explore_memberships(row)
    captured = metadata_capture_datetime(row)
    timeline_filename = filename.strip()
    # The full accumulator never evaluates likes on undated/nameless rows,
    # and evaluates future rows only once they become eligible. Preserve the
    # exceptional int(infinity) case until rendering that day, too.
    score = (1, 0, timeline_filename)
    if captured is not None and timeline_filename:
        try:
            score = _cover_score(row, timeline_filename)
        except OverflowError:
            score = (-1, 0, timeline_filename)
    return Contribution(identity, position, filename, tuple(memberships),
                        captured.date().toordinal() if captured else None,
                        timeline_filename, score[0], score[1])


class ContributionConflict(ValueError):
    """The supplied before state/version does not match the local sidecar."""


def _encode(value: Contribution) -> str:
    if not isinstance(value.identity, str) or not value.identity:
        raise ValueError('identity must be a nonempty string')
    if type(value.position) is not int or not 0 <= value.position < 2**63:
        raise ValueError('position must be a nonnegative SQLite integer')
    if value.capture_day is not None:
        date.fromordinal(value.capture_day)
    if value.liked_order not in (-1, 0, 1) or not 0 <= value.cover_rank <= 0xffffffff:
        raise ValueError('invalid streaming timeline cover score')
    if any(m.kind not in ('places', 'things') or not m.key for m in value.memberships):
        raise ValueError('invalid Explore membership')
    return json.dumps(asdict(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class SummaryContributionStore:
    """A local SQLite sidecar, one library and one explicit source version.

    Writers are serialized by BEGIN IMMEDIATE. A batch (including its source
    version) commits atomically. Memory is O(one contribution + affected keys)
    per change; summaries read distinct day buckets and indexed top groups,
    never all photos. Use a real local filesystem, not SMB.
    """

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, timeout=60)
        self._conn.execute('PRAGMA cache_size=-4096')
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS photos(
                identity TEXT PRIMARY KEY, position INTEGER UNIQUE NOT NULL, contribution TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS members(
                identity TEXT NOT NULL, occurrence INTEGER NOT NULL,
                kind TEXT NOT NULL, key TEXT NOT NULL, position INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(identity, occurrence));
            CREATE INDEX IF NOT EXISTS member_candidates ON members(kind, key, position, occurrence);
            CREATE TABLE IF NOT EXISTS groups(
                kind TEXT NOT NULL, key TEXT NOT NULL, count INTEGER NOT NULL,
                first_position INTEGER NOT NULL, first_occurrence INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(kind, key));
            CREATE INDEX IF NOT EXISTS top_groups
                ON groups(kind, count DESC, first_position, first_occurrence);
            CREATE TABLE IF NOT EXISTS days(day INTEGER PRIMARY KEY, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS covers(
                identity TEXT PRIMARY KEY, day INTEGER NOT NULL, liked_order INTEGER NOT NULL,
                rank INTEGER NOT NULL, filename TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS day_candidates ON covers(day, liked_order, rank, filename);
        ''')
        stored = self._conn.execute("SELECT value FROM meta WHERE key='format'").fetchone()
        if stored and stored[0] != FORMAT:
            self.close()
            raise ValueError('unsupported summary sidecar format')
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO meta VALUES('format', ?)", (FORMAT,))

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _version(self):
        row = self._conn.execute("SELECT value FROM meta WHERE key='source'").fetchone()
        return row[0] if row else None

    def get(self, identity: str) -> Optional[Contribution]:
        row = self._conn.execute('SELECT contribution FROM photos WHERE identity=?', (identity,)).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        value['memberships'] = tuple(Membership(**m) for m in value['memberships'])
        return Contribution(**value)

    def seed(self, contributions: Iterable[Contribution], *, source_version: str):
        """Explicit single-pass bootstrap, only on an unseeded sidecar.

        Caller certifies a complete, ordered stream. Interrupted/duplicate
        streams roll back entirely; an existing seeded state is never reset.
        """
        self._check_version_string(source_version)
        conn = self._conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            if self._version() is not None:
                raise ContributionConflict('already seeded; use a new sidecar for a full reseed')
            for contribution in contributions:
                self._replace(None, contribution)
            conn.execute("INSERT INTO meta VALUES('source', ?)", (source_version,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _check_version_string(version):
        if not isinstance(version, str) or not version:
            raise ValueError('a nonempty explicit source version is required')

    def apply(self, changes: Iterable[tuple[Optional[Contribution], Optional[Contribution]]], *,
              previous_source_version: str, source_version: str):
        """Apply explicit before/after pairs; None denotes insertion/deletion.

        No dirty-ID discovery or fallback scan. Even deletion requires its
        exact stored before contribution. A stale/replayed batch fails closed.
        """
        self._check_version_string(previous_source_version)
        self._check_version_string(source_version)
        if previous_source_version == source_version:
            raise ValueError('source version must advance')
        conn = self._conn
        conn.execute('BEGIN IMMEDIATE')
        try:
            if self._version() != previous_source_version:
                raise ContributionConflict('unseeded or stale source version')
            for before, after in changes:
                self._replace(before, after)
            conn.execute("UPDATE meta SET value=? WHERE key='source'", (source_version,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _replace(self, before, after):
        if before is None and after is None:
            raise ValueError('empty change')
        if before is not None and after is not None and before.identity != after.identity:
            raise ValueError('identity changes require separate deletion and insertion')
        identity = (before if before is not None else after).identity
        before_json = _encode(before) if before is not None else None
        after_json = _encode(after) if after is not None else None
        conn = self._conn
        stored = conn.execute('SELECT contribution FROM photos WHERE identity=?', (identity,)).fetchone()
        if (stored[0] if stored else None) != before_json:
            raise ContributionConflict('before contribution mismatch: ' + identity)
        affected = set()
        for value, sign in ((before, -1), (after, 1)):
            if value is None:
                continue
            day = value.capture_day or 0
            conn.execute('INSERT INTO days(day,count) VALUES(?,?) '
                         'ON CONFLICT(day) DO UPDATE SET count=count+excluded.count', (day, sign))
            conn.execute('DELETE FROM days WHERE day=? AND count=0', (day,))
            for m in value.memberships:
                affected.add((m.kind, m.key))
                # Keep first-candidate fields until the final indexed repair.
                conn.execute('INSERT INTO groups VALUES(?,?,?,0,0,?) '
                             'ON CONFLICT(kind,key) DO UPDATE SET count=count+excluded.count',
                             (m.kind, m.key, sign, '{}'))
        conn.execute('DELETE FROM photos WHERE identity=?', (identity,))
        conn.execute('DELETE FROM members WHERE identity=?', (identity,))
        conn.execute('DELETE FROM covers WHERE identity=?', (identity,))
        if after is not None:
            conn.execute('INSERT INTO photos VALUES(?,?,?)', (identity, after.position, after_json))
            for occurrence, m in enumerate(after.memberships):
                payload = {'label': m.label, 'filename': after.filename}
                if m.kind == 'places':
                    payload.update(latitude=m.latitude, longitude=m.longitude)
                conn.execute('INSERT INTO members VALUES(?,?,?,?,?,?)',
                             (identity, occurrence, m.kind, m.key, after.position,
                              json.dumps(payload, ensure_ascii=False)))
            if after.capture_day is not None and after.timeline_filename:
                conn.execute('INSERT INTO covers VALUES(?,?,?,?,?)',
                             (identity, after.capture_day, after.liked_order, after.cover_rank,
                              after.timeline_filename))
        for kind, key in affected:
            conn.execute('DELETE FROM groups WHERE kind=? AND key=? AND count=0', (kind, key))
            first = conn.execute('SELECT position, occurrence, payload FROM members WHERE kind=? AND key=? '
                                 'ORDER BY position, occurrence LIMIT 1', (kind, key)).fetchone()
            if first:
                conn.execute('UPDATE groups SET first_position=?,first_occurrence=?,payload=? WHERE kind=? AND key=?',
                             (*first, kind, key))

    def _require_seed(self):
        if self._version() is None:
            raise ContributionConflict('explicit full-stream seed required')

    def explore_groups(self, *, limit: int = 24) -> dict:
        """Exact pre-hydration winners, including first-seen labels/coordinates.

        No minimum count threshold exists in ExploreAccumulator. Its stable
        count-descending sort resolves ties by first surviving stream occurrence.
        """
        if type(limit) is not int or limit < 0:
            raise ValueError('limit must be a nonnegative integer')
        with self._conn:
            self._conn.execute('BEGIN')
            self._require_seed()
            return self._explore_groups(limit)

    def _explore_groups(self, limit):
        return {kind: [dict(json.loads(payload), count=count) for count, payload in self._conn.execute(
            'SELECT count,payload FROM groups WHERE kind=? '
            'ORDER BY count DESC,first_position,first_occurrence LIMIT ?', (kind, limit))]
            for kind in ('places', 'things')}

    def explore_summary(self, *, fetch_metadata: Callable, build_photo: Callable) -> dict:
        """Hydrate <=48 winners; callbacks preserve the app's photo projection.

        fetch_metadata(filename), build_photo(filename, metadata). Missing
        metadata drops an already-selected winner, without backfilling, exactly
        as ExploreAccumulator.finalize. Caller supplies user/people-name context.
        """
        groups = self.explore_groups()
        fetched = {name: fetch_metadata(name) for name in dict.fromkeys(
            item['filename'] for items in groups.values() for item in items)}
        result = {}
        for kind, items in groups.items():
            result[kind] = []
            for item in items:
                metadata = fetched[item['filename']]
                if not metadata:
                    continue
                entry = {k: v for k, v in item.items() if k != 'filename'}
                entry['photo'] = build_photo(item['filename'], metadata)
                result[kind].append(entry)
        return result

    def timeline_summary(self, *, now: Optional[datetime] = None) -> dict:
        """Exact TimelineAccumulator output, including date rollover.

        Visits distinct day counts, then one indexed candidate per admitted
        day. RAM is O(output day buckets), not O(photos/group membership).
        """
        today = (now or datetime.now(timezone.utc)).date()
        conn = self._conn
        with conn:
            conn.execute('BEGIN')
            self._require_seed()
            years, year_covers, month_covers = {}, {}, {}
            total = undated = future = 0
            first = last = None
            for ordinal, count in conn.execute('SELECT day,count FROM days ORDER BY day'):
                total += count
                if ordinal == 0:
                    undated += count
                    continue
                day = date.fromordinal(ordinal)
                if day > today:
                    future += count
                    continue
                year, month, day_key = f'{day.year:04d}', f'{day.month:02d}', f'{day.day:02d}'
                y = years.setdefault(year, {'count': 0, 'months': {}})
                m = y['months'].setdefault(month, {'count': 0, 'days': {}})
                y['count'] += count
                m['count'] += count
                m['days'][day_key] = count
                first = day if first is None else min(first, day)
                last = day if last is None else max(last, day)
                score = conn.execute('SELECT liked_order,rank,filename FROM covers WHERE day=? '
                                     'ORDER BY liked_order,rank,filename LIMIT 1', (ordinal,)).fetchone()
                if score:
                    if score[0] == -1:
                        raise OverflowError('cannot convert float infinity to integer')
                    year_covers[year] = min(score, year_covers.get(year, score))
                    month_covers[year, month] = min(score, month_covers.get((year, month), score))
            cumulative, running = {}, 0
            for year in sorted(years):
                y = years[year]
                running += y['count']
                cumulative[year] = running
                if year in year_covers:
                    y['coverFilename'] = year_covers[year][2]
                for month, m in y['months'].items():
                    if (year, month) in month_covers:
                        m['coverFilename'] = month_covers[year, month][2]
            return {'years': years, 'cumulativeByYear': cumulative,
                    'firstDate': first.isoformat() if first else None,
                    'lastDate': last.isoformat() if last else None, 'today': today.isoformat(),
                    'undatedCount': undated, 'futureCount': future, 'totalCount': total}