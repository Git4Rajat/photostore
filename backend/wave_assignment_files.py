"""Append-only wave journals on the trusted checkpoint mount, indexed locally.

No live SQLite database is placed on SMB. The immutable input snapshot is
flushed before publishing one Table marker, and each decision is flushed before
its materialization. Missing/corrupt marked journals fail closed: never capture
a partially materialized library as a fresh baseline. Old Table journals remain
readable and continue through their original implementation.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3

import processing_diagnostics

from upload_waves import digest
from wave_assignment import WaveAssignmentJournal, encoded

_MAX_LINE = 256_000


class FileAssignmentJournal(WaveAssignmentJournal):
    def __init__(self, store, wave_id, check, directory, local):
        super().__init__(store, wave_id, check)
        self.cloud = WaveAssignmentJournal(store, wave_id, check)
        self.db = sqlite3.connect(str(Path(local) / 'journal.sqlite3'))
        self.db.execute('PRAGMA cache_size=-512')
        self.db.execute('CREATE TABLE journal(key TEXT PRIMARY KEY, body TEXT, digest TEXT)')
        self.file = None
        self.preparing = False
        try:
            marker = self.cloud.get('local-v1')
            path = directory / 'journal.jsonl'
            if path.is_symlink():
                raise ValueError('Wave journal must not be a symlink')
            if marker is not None and not path.is_file():
                raise ValueError('Marked wave journal is missing; restore the mounted journal before retrying')
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            self.file = os.fdopen(fd, 'ab+')
            if marker is None:
                # No materialization is permitted before the snapshot marker.
                # An interrupted unpublished capture can safely start over.
                self.file.truncate(0)
            else:
                self._restore()
                header = self.get('manifest')
                if marker != self._marker(header):
                    raise ValueError('Mounted wave journal does not match its durable marker')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _marker(header):
        if not isinstance(header, dict):
            raise ValueError('Wave journal has no complete frozen manifest')
        return dict(format=1, manifestDigest=digest(header), binding=header['binding'])

    def _restore(self):
        self.file.seek(0)
        while True:
            position = self.file.tell()
            line = self.file.readline(_MAX_LINE + 1)
            if not line:
                break
            if len(line) > _MAX_LINE:
                raise ValueError('Oversized wave journal record')
            if not line.endswith(b'\n'):
                # A torn final append cannot have returned success to its writer.
                self.file.truncate(position)
                self.file.flush()
                os.fsync(self.file.fileno())
                break
            row = json.loads(line)
            if digest(row['payload']) != row['digest']:
                raise ValueError('Corrupt mounted wave journal')
            self._index(row['key'], row['payload'], row['digest'])
        self.db.commit()
        self.file.seek(0, os.SEEK_END)

    def _index(self, key, payload, identity):
        old = self.get(key)
        if old is not None:
            if old != payload:
                raise ValueError('Immutable mounted wave journal rebound')
            return
        self.db.execute('INSERT INTO journal VALUES (?,?,?)', (key, encoded(payload), identity))

    def get(self, key):
        row = self.db.execute('SELECT body,digest FROM journal WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        if digest(payload) != row[1]:
            raise ValueError('Corrupt local wave journal index')
        return payload

    def _flush(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.db.commit()

    @processing_diagnostics.wave_phase('journal_write')
    def put(self, key, payload):
        self.check()
        body = encoded(payload)
        if len(body.encode('utf-16-le')) > 60_000:
            raise ValueError('Assignment journal row exceeds byte budget')
        identity = digest(payload)
        previous = self.get(key)
        if previous is not None:
            if previous != json.loads(body):
                raise ValueError('Immutable mounted wave journal rebound')
            return identity
        record = encoded(dict(key=key, payload=json.loads(body), digest=identity)).encode() + b'\n'
        if len(record) > _MAX_LINE:
            raise ValueError('Oversized wave journal record')
        self.file.write(record)
        self._index(key, json.loads(body), identity)
        if not self.preparing or key == 'manifest':
            self._flush()
        if key == 'manifest':
            # The snapshot is durable on the mount before any Table effects.
            self.cloud.put('local-v1', self._marker(json.loads(body)))
        return identity

    def rows(self, kind):
        prefix = kind + '!'
        cursor = self.db.execute('SELECT body,digest FROM journal WHERE key>=? AND key<? ORDER BY key',
                                 (prefix, prefix + '~'))
        for body, identity in cursor:
            payload = json.loads(body)
            if digest(payload) != identity:
                raise ValueError('Corrupt local wave journal index')
            yield payload

    def prepare(self, *args, **kwargs):
        self.preparing = True
        try:
            return super().prepare(*args, **kwargs)
        finally:
            self.preparing = False

    def finish(self, header, chain):
        identity = super().finish(header, chain)
        # Keep a compact durable completion receipt for publication/diagnostics.
        if self.cloud.put('complete', self.get('complete')) != identity:
            raise ValueError('Wave completion receipt mismatch')
        return identity

    def close(self):
        try:
            if self.file is not None:
                self.file.close()
        finally:
            self.db.close()


@contextmanager
def assignment_journal(store, wave_id, check, *, share, local):
    cloud = WaveAssignmentJournal(store, wave_id, check)
    if not share or cloud.get('manifest') is not None:
        yield cloud
        return
    root = Path(share)
    directory = root / 'wave-journals' / digest([store.library, wave_id])
    if root.is_symlink() or (root / 'wave-journals').is_symlink() or directory.is_symlink():
        raise ValueError('Wave journal directories must not be symlinks')
    directory.mkdir(parents=True, exist_ok=True)
    lock = os.open(directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        # Same cross-process mount contract as clustering_checkpoint.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = FileAssignmentJournal(store, wave_id, check, directory, local)
        try:
            yield journal
        finally:
            journal.close()
    finally:
        os.close(lock)
