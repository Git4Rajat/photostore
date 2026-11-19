"""Estimated, durable progress for the background stages of an upload wave.

Vision counts members as its page commits land; cluster and index only have a
state and a start time, so they report no fraction. Progress rows live in the
library's wave partition next to the wave rows, are advisory, and are written
without compare-and-swap so they never contend with wave transitions.
"""
import time

from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableTransactionError

from upload_waves import identifier

STEPS = 10
MIN_WRITE_SECONDS = 10
RATE_ALPHA = 0.3
MIN_SAMPLES = 3
SMALL_WAVE = 50
RETAIN_SECONDS = 24 * 3600
# Nothing moved for this long while the stage is not failed: say so instead of "in progress".
STALL_SECONDS = 600
MAX_ROWS = 200
STAGE_OF_STATE = {'processing_pending': 'vision', 'processing': 'vision',
                  'ready': 'cluster', 'indexing': 'index'}
# Coarse buckets, in seconds: the rounding is what keeps a rough estimate honest.
ETA_BUCKETS = (60, 120, 300, 600, 900, 1800, 3600, 7200)


def step_of(done, total):
    if total <= 0:
        return 0
    return min(STEPS, (done * STEPS) // total)


def eta_bucket(seconds):
    if seconds is None:
        return None
    for bucket in ETA_BUCKETS:
        if seconds <= bucket:
            return bucket
    return ETA_BUCKETS[-1]


def problem_of(health):
    """What the person should be told is wrong, from wave_health.status()'s health block."""
    if health.get('state') == 'failed':
        return 'failed'
    if health.get('state') == 'blocked':
        return 'blocked'
    if health.get('state') in ('pending', 'processing') and float(health.get('noProgressSeconds') or 0) >= STALL_SECONDS:
        return 'stalled'
    return None


def progress_key(wave_id):
    return 'progress!' + identifier(wave_id)


def dismiss_key(wave_id, user):
    return 'progressdismiss!' + identifier(wave_id) + '!' + identifier(user)


class ProgressStore:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock
        self._seen = {}  # wave -> last row this process wrote, so page commits skip the read

    def _upsert(self, key, **fields):
        """Advisory write: one writer per wave, so a lost ETag race just drops this sample."""
        old = self.store._get(key)
        try:
            self.store.table.submit_transaction([self.store._change(old, self.store._row(key, **fields))])
        except (ResourceExistsError, ResourceModifiedError, TableTransactionError):
            pass

    def record_vision(self, wave_id, done, total, *, force=False):
        """Writes vision progress; returns (row, step_crossed). Throttled, so most page
        commits return the cached row without a write."""
        now = self.clock()
        key = progress_key(wave_id)
        previous = self._seen.get(wave_id) or self.store._get(key) or {}
        old_step = int(previous.get('step') or 0)
        step = step_of(done, total)
        if (not force and step == old_step
                and now - float(previous.get('updatedAt') or 0) < MIN_WRITE_SECONDS):
            return dict(previous, RowKey=key), False
        started = float(previous.get('stageStartedAt') or now)
        rate = float(previous.get('rate') or 0)
        samples = int(previous.get('samples') or 0)
        elapsed = now - float(previous.get('updatedAt') or started)
        moved = done - int(previous.get('done') or 0)
        if previous and elapsed > 0 and moved > 0:
            sample = moved / elapsed
            rate = sample if not rate else rate + RATE_ALPHA * (sample - rate)
            samples += 1
        eta = None
        if samples >= MIN_SAMPLES and rate > 0 and total > done:
            eta = eta_bucket((total - done) / rate)
        row = dict(stage='vision', done=done, total=total, step=step, rate=rate, samples=samples,
                   etaSeconds=eta if eta is not None else -1, stageStartedAt=started, updatedAt=now)
        self._upsert(key, **row)
        self._seen[wave_id] = row
        return dict(row, RowKey=key), step != old_step and (total >= SMALL_WAVE or force)

    def _health(self, wave_id, wave):
        """Advisory: a health read that fails just means no problem is reported."""
        out = dict(failedMembers=int(wave.get('failedMembers') or 0))
        try:
            from wave_health import status
            health = status(self.store, wave_id)['health']
        except Exception:
            return out
        problem = problem_of(health)
        if problem:
            out.update(problem=problem, noProgressSeconds=int(float(health.get('noProgressSeconds') or 0)),
                       dispatchAttempts=int(health.get('dispatchAttempts') or 0))
            if problem == 'blocked':
                out['blockedBy'] = health.get('blockedByWaveId')
        return out

    def dismiss(self, wave_id, user):
        self._upsert(dismiss_key(wave_id, user), waveId=wave_id, dismissedAt=self.clock())

    def _dismissed(self, wave_id, user):
        try:
            self.store.table.get_entity(self.store.partition, dismiss_key(wave_id, user))
            return True
        except ResourceNotFoundError:
            return False

    def active(self, user):
        """Waves worth showing: still running, or finished within the retention window and
        not dismissed by this user. Stages read from the wave state, so a wave whose vision
        stage wrote nothing (no faces, empty) still appears while it indexes."""
        now = self.clock()
        out = []
        for row in self.store._page('progress!', size=MAX_ROWS):
            if now - float(row.get('updatedAt') or 0) > RETAIN_SECONDS:
                continue
            wave_id = row['RowKey'][len('progress!'):]
            wave = self.store._get(self.store.wave_key(wave_id))
            if not wave or wave['state'] == 'cancelled':
                continue
            complete = wave['state'] == 'complete'
            if complete and self._dismissed(wave_id, user):
                continue
            stage = 'done' if complete else STAGE_OF_STATE.get(wave['state'])
            if stage is None:
                continue
            item = dict(waveId=wave_id, stage=stage, state=wave['state'],
                        startedAt=float(wave.get('createdAt') or 0),
                        stageEnteredAt=float(wave.get('stateEnteredAt') or 0))
            if stage == 'vision':
                item.update(done=int(row.get('done') or 0), total=int(row.get('total') or 0),
                            etaSeconds=None if int(row.get('etaSeconds', -1)) < 0 else int(row['etaSeconds']))
            if not complete:
                item.update(self._health(wave_id, wave))
            if complete:
                item.update(processedSeconds=max(0, round(float(wave['stateEnteredAt']) - float(
                    wave.get('frozenAt') or wave['createdAt']))), failed=int(wave.get('failedMembers') or 0))
            out.append(item)
        return out
