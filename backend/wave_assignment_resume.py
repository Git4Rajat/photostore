"""Optional acceleration snapshots for mounted wave journals.

The journal remains authoritative. Publish a resume marker LAST, after all
applied markers, a closed workspace backup and a native runtime checkpoint.
Missing/stale/corrupt acceleration state falls back to canonical journal replay.
I/O/ownership failures propagate. No active SQLite connection uses the share.
The caller holds the wave journal's cross-process mount lock throughout.
"""
import hashlib
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from clustering_runtime import LiveFaceIndex
import processing_diagnostics
from upload_waves import digest


def _checksum(path):
    if path.is_symlink():
        raise ValueError('Wave resume snapshot must not be a symlink')
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def save(journal, workspace, runtime, header, position, chain, local):
    if not hasattr(journal, 'directory'):
        return
    journal.check()
    directory = journal.directory
    target = directory / 'workspace.snapshot'
    if target.is_symlink():
        raise ValueError('Wave resume snapshot must not be a symlink')
    backup = Path(local) / 'resume.sqlite3'
    workspace.db.commit()
    with closing(sqlite3.connect(backup)) as destination:
        workspace.db.backup(destination)
    revision = ['wave-resume-v1', digest(header), position, chain, _checksum(backup)]
    # Existing checkpoint code validates native slot layout, SQLite integrity,
    # configuration and checksums without training/rebuilding on restoration.
    runtime.save_checkpoint(directory / 'resume-runtime', source_revision=revision)
    temporary = directory / ('.workspace-' + uuid.uuid4().hex)
    try:
        with backup.open('rb') as source, temporary.open('xb') as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
        journal.check()
        journal.put(f'resume!{position:012d}', dict(manifestId=digest(header),
            position=position, chain=chain, workspaceDigest=revision[-1]))
        # This checkpoint directory is private to this wave and every reader
        # holds the parent journal lock. Retain the current/two recent complete
        # generations without accumulating a day's worth of large slice copies.
        from clustering_checkpoint import cleanup_checkpoints
        cleanup_checkpoints(directory / 'resume-runtime', grace_seconds=0)
    finally:
        temporary.unlink(missing_ok=True)


def restore(journal, workspace, binding, local, config):
    if not hasattr(journal, 'directory'):
        return None
    header = journal.get('manifest')
    if header is None:
        return None
    if header.get('binding') != binding:
        raise ValueError('Frozen assignment manifest or matcher policy changed')
    row = journal.db.execute("SELECT key FROM journal WHERE key>=? AND key<? ORDER BY key DESC LIMIT 1",
                             ('resume!', 'resume!~')).fetchone()
    if row is None:
        return None
    marker = journal.get(row[0])
    position = marker.get('position')
    if (marker.get('manifestId') != digest(header) or type(position) is not int
            or not 0 < position < header['faceCount']):
        raise ValueError('Invalid wave resume journal marker')
    source = journal.directory / 'workspace.snapshot'
    backup = Path(local) / 'restored-workspace.sqlite3'
    runtime = None
    try:
        # Validate the LOCAL copy actually used. A later failed save can leave
        # a workspace/checkpoint newer than the last published marker.
        if source.is_symlink():
            raise ValueError('Wave resume snapshot must not be a symlink')
        try:
            stream = source.open('rb')
        except FileNotFoundError:
            return None  # Only an absent optional snapshot is a cache miss.
        with stream as src, backup.open('xb') as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
        if _checksum(backup) != marker['workspaceDigest']:
            return None
        revision = ['wave-resume-v1', digest(header), position, marker['chain'], marker['workspaceDigest']]
        runtime = LiveFaceIndex.restore_checkpoint(journal.directory / 'resume-runtime',
            Path(local) / 'restored-runtime', source_revision=revision, config=config.index_config,
            candidates=config.candidates, nprobe=config.nprobe,
            delta_limit=config.delta_limit, threads=config.threads)
        if runtime is None:
            return None
        with closing(sqlite3.connect(backup)) as restored:
            if restored.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('Corrupt wave resume workspace')
            restored.backup(workspace.db)
        journal.check()
        processing_diagnostics.emit('cluster_resume_restored', wave_id=journal.wave_id,
                                    processed_faces=position)
        return header, runtime, position, marker['chain']
    except BaseException:
        if runtime is not None:
            runtime.close()
        raise
