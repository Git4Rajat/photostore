"""Attempt-local, byte-bounded planning buffers; never source authority."""
from collections import OrderedDict, deque
import os
import pickle
import sqlite3
import tempfile
import time

import processing_metrics as metrics


def limit_bytes(name, default_mib, maximum_mib):
    value = int(os.getenv(name, str(default_mib)))
    if not 0 <= value <= maximum_mib:
        raise ValueError('Invalid Vision buffer setting: ' + name)
    return value * 1024 * 1024


class Snapshot(dict):
    def __init__(self, row, metadata):
        super().__init__(row)
        self.metadata = metadata


def encode(row):
    return pickle.dumps((dict(row), dict(getattr(row, 'metadata', {}))), protocol=5)


def decode(data):
    return Snapshot(*pickle.loads(data))


class LocalRows:
    def __init__(self, *, prefix, memory_bytes, disk_bytes):
        if prefix not in ('receipt_cache', 'member_buffer') or memory_bytes < 0 or disk_bytes < 0:
            raise ValueError('Invalid local row buffer limits')
        self.prefix, self.memory_limit, self.disk_limit = prefix, memory_bytes, disk_bytes
        self.entries = OrderedDict()
        self.memory_bytes = self.disk_bytes = 0
        self.db = self.directory = None

    def __contains__(self, key):
        return key in self.entries

    def __len__(self):
        return len(self.entries)

    def _database(self):
        if self.db is None:
            self.directory = tempfile.TemporaryDirectory(prefix='vision-planning-')
            os.chmod(self.directory.name, 0o700)
            self.db = sqlite3.connect(os.path.join(self.directory.name, 'rows.sqlite'))
            self.db.execute('pragma journal_mode=off')
            self.db.execute('pragma synchronous=off')
            self.db.execute('pragma cache_size=-4096')
            self.db.execute('pragma max_page_count=' + str(max(16, self.disk_limit // 4096)))
            self.db.execute('create table rows (key text primary key, data blob)')
        return self.db

    def put(self, key, row):
        data = encode(row)
        if key in self.entries:
            self.pop(key)
        if self.memory_bytes + len(data) <= self.memory_limit:
            self.entries[key] = (data, len(data))
            self.memory_bytes += len(data)
        elif self.disk_bytes + len(data) <= self.disk_limit * 7 // 10:
            started = time.monotonic()
            try:
                db = self._database()
                db.execute('insert into rows values (?, ?)', (key, data))
            except (OSError, sqlite3.Error):
                metrics.add(**{self.prefix + '_disk_errors': 1})
                self.disable_disk()
                return False
            finally:
                metrics.add(**{self.prefix + '_disk_write_ms': (time.monotonic() - started) * 1000})
            self.entries[key] = (None, len(data))
            self.disk_bytes += len(data)
        else:
            return False
        metrics.observe(**{self.prefix + '_memory_peak_bytes': self.memory_bytes,
                           self.prefix + '_disk_peak_bytes': self.disk_bytes,
                           self.prefix + '_entries_peak': len(self.entries)})
        return True

    def flush(self):
        if self.db is not None:
            try:
                self.db.commit()
                metrics.observe(**{self.prefix + '_disk_file_peak_bytes': os.path.getsize(
                    os.path.join(self.directory.name, 'rows.sqlite'))})
            except (OSError, sqlite3.Error):
                metrics.add(**{self.prefix + '_disk_errors': 1})
                self.disable_disk()

    def disable_disk(self):
        for key in list(self.entries):
            if self.entries[key][0] is None:
                del self.entries[key]
        self.disk_bytes = self.disk_limit = 0
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def get(self, key):
        data, _ = self.entries[key]
        if data is None:
            started = time.monotonic()
            try:
                data = self.db.execute('select data from rows where key=?', (key,)).fetchone()[0]
            except (sqlite3.Error, TypeError):
                metrics.add(**{self.prefix + '_disk_errors': 1})
                raise KeyError(key)
            finally:
                metrics.add(**{self.prefix + '_disk_read_ms': (time.monotonic() - started) * 1000})
            metrics.add(**{self.prefix + '_disk_hits': 1})
        self.entries.move_to_end(key)
        try:
            return decode(data)
        except (pickle.UnpicklingError, EOFError, TypeError, ValueError):
            metrics.add(**{self.prefix + '_disk_errors': 1})
            raise KeyError(key)

    def pop(self, key):
        if key not in self.entries:
            return
        data, size = self.entries.pop(key)
        if data is None:
            self.disk_bytes -= size
            try:
                self.db.execute('delete from rows where key=?', (key,))
            except sqlite3.Error:
                metrics.add(**{self.prefix + '_disk_errors': 1})
                self.disable_disk()
        else:
            self.memory_bytes -= size

    def clear(self):
        self.entries.clear()
        self.memory_bytes = self.disk_bytes = 0
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.clear()


class MemberWindow:
    def __init__(self, store, wave_id, *, fetch_size=1000, memory_bytes=None, disk_bytes=None):
        if type(fetch_size) is not int or not 20 <= fetch_size <= 1000:
            raise ValueError('Vision member fetch size must be between 20 and 1000')
        self.store, self.prefix, self.fetch_size = store, f'e!{wave_id}!m!', fetch_size
        self.rows = LocalRows(prefix='member_buffer',
            memory_bytes=limit_bytes('IPWORK_VISION_MEMBER_MEMORY_MB', 1, 64) if memory_bytes is None else memory_bytes,
            disk_bytes=limit_bytes('IPWORK_VISION_MEMBER_DISK_MB', 64, 1024) if disk_bytes is None else disk_bytes)
        self.keys = deque()
        self.done = self.last_fetch = False

    def page(self, after):
        if not self.keys:
            with metrics.timer('vision_member_fetch', count_name='vision_member_fetch_calls'):
                rows = self.store._page(self.prefix, after, size=self.fetch_size)
            metrics.add(vision_member_rows_fetched=len(rows))
            self.last_fetch = len(rows) < self.fetch_size
            fetched_all = self.last_fetch
            for row in rows:
                if not self.rows.put(row['RowKey'], row):
                    self.last_fetch = False
                    metrics.add(member_buffer_capacity_stops=1)
                    self.fetch_size = max(20, len(self.keys))
                    if len(self.keys) < 20:
                        self.keys.clear()
                        self.rows.clear()
                        self.done = fetched_all and len(rows) <= 20
                        return rows[:20]
                    break
                self.keys.append(row['RowKey'])
            self.rows.flush()
        result = []
        while self.keys and len(result) < 20:
            key = self.keys.popleft()
            try:
                result.append(self.rows.get(key))
            except KeyError:
                self.keys.clear()
                self.rows.clear()
                metrics.add(member_buffer_reloads=1)
                return self.page(after)
            self.rows.pop(key)
        self.rows.flush()
        self.done = self.last_fetch and not self.keys
        metrics.add(vision_member_groups=len(result) > 0)
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.rows.clear()
