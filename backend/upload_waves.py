"""Transactional shadow intake/barrier protocol. No queue or worker dependencies.

Rows share one library partition in the existing jobs table, outside job-status
partitions. Every membership/evidence mutation fences the control row. A paged
scan may publish shadow readiness only against its original control revision.
This observational protocol MUST NOT be used as a production stage release.
"""
from __future__ import annotations

import hashlib
import json
import re
import random
import time
import uuid
from itertools import islice

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableTransactionError, UpdateMode

MAX_PAGE = 20
MAX_RETRIES = 8
EMPTY_DIGEST = hashlib.sha256(b'upload-wave-shadow-v1').hexdigest()


class Conflict(RuntimeError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Expected a stable identifier of 1-128 letters, digits, underscores or hyphens')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _etag(row):
    value = getattr(row, 'metadata', {}).get('etag')
    if not value:
        raise RuntimeError('Wave protocol requires genuine entity ETags')
    return value


class ShadowWaves:
    def __init__(self, table, library, *, clock=time.time):
        if table is None or not library:
            raise ValueError('Wave store requires a table and authenticated library')
        self.table, self.library, self.clock = table, library, clock
        self.partition = 'upload-wave-shadow-' + digest(library)

    def _row(self, key, **fields):
        return dict(PartitionKey=self.partition, RowKey=key, libraryId=self.library,
                    protocolMode='shadow', **fields)

    def _get(self, key):
        try:
            return self.table.get_entity(self.partition, key)
        except ResourceNotFoundError:
            return None

    def _required(self, key):
        row = self._get(key)
        if row is None:
            raise Conflict('Unknown session, member or wave')
        return row

    def _change(self, old, new):
        if old is None:
            return ('create', new)
        return ('update', new, dict(mode=UpdateMode.REPLACE, etag=_etag(old),
                                    match_condition=MatchConditions.IfNotModified))

    def _commit(self, control, changes, *, advance=True):
        new = dict(control or self._row('control', revision='', collectingWaveId='', tailWaveId=''))
        if advance:
            new.update(revision=uuid.uuid4().hex, updatedAt=self.clock())
        self.table.submit_transaction([self._change(control, new), *changes])

    def _retry(self, action):
        for attempt in range(MAX_RETRIES):
            try:
                return action()
            except (ResourceExistsError, ResourceModifiedError):
                pass
            except TableTransactionError as error:
                if error.status_code not in (409, 412):
                    raise
            if attempt + 1 == MAX_RETRIES:
                raise Conflict('Concurrent wave mutation; retry the request')
            # Immediate retries can repeatedly lose to another request on the
            # library control row. Yield with bounded jitter; still fail closed
            # after MAX_RETRIES rather than weakening the conditional commit.
            time.sleep(random.uniform(0.001, min(0.05, 0.002 * (2 ** attempt))))

    def _page(self, prefix, after='', *, size=MAX_PAGE):
        # RowKey range + service paging: never materialize an entire library.
        query = (f"PartitionKey eq '{self.partition}' and RowKey ge '{prefix}' "
                 f"and RowKey lt '{prefix}~'")
        if after:
            query += f" and RowKey gt '{after}'"
        rows = self.table.query_entities(query, results_per_page=size)
        return list(islice(rows, size))

    @staticmethod
    def session_key(session):
        return 'session!' + identifier(session)

    @staticmethod
    def wave_key(wave):
        return 'wave!' + identifier(wave)

    @staticmethod
    def item_key(session, item):
        return 'item!' + identifier(session) + '!' + identifier(item)

    def accept_source_completion(self, intent):
        """Durably/idempotently accept an intent, without asserting readiness.

        This inbox is independent of admission timing: completion may precede
        the upload item's resolve request. Artifact qualification is separate.
        """
        from processing_completion import validate_intent
        payload = validate_intent(intent, self.library)
        artifacts_qualified = False
        if payload.get('artifactReceipt') is not None:
            from processing_receipts import ArtifactReceipts
            ArtifactReceipts(self.table, self.library).verify_reference(payload['artifactReceipt'], payload)
            artifacts_qualified = True
        body = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        key = 'source-completion!' + digest([payload['photoId'], payload['revision']]) + '!' + intent['id']
        def run():
            previous = self._get(key)
            if previous:
                if previous.get('payload') != body:
                    raise Conflict('Source completion identity cannot be rebound')
                return dict(previous)
            row = self._row(key, completionId=intent['id'], photoId=payload['photoId'],
                processingRevision=payload['revision'], payload=body, completionQualified=False, artifactReceiptQualified=artifacts_qualified,
                acceptedAt=self.clock())
            self.table.submit_transaction([('create', row)])
            return row
        return self._retry(run)

    def open_session(self, session, declared_count, *, resumed_from=''):
        key = self.session_key(session)
        if resumed_from:
            identifier(resumed_from)
            if resumed_from == session:
                raise ValueError('A session cannot resume itself')
        if type(declared_count) is not int or not 0 <= declared_count <= 1_000_000:
            raise ValueError('declaredCount must be an integer between 0 and 1000000')

        def run():
            existing = self._get(key)
            if existing:
                if existing['declaredCount'] != declared_count or existing.get('resumedFromSessionId', '') != resumed_from:
                    raise Conflict('A session declaration is immutable')
                return dict(existing)
            if resumed_from:
                self._required(self.session_key(resumed_from))
            control = self._get('control')
            wave_id = (control or {}).get('collectingWaveId') or uuid.uuid4().hex
            old_wave = self._get(self.wave_key(wave_id))
            predecessor = self._get(self.wave_key(control['tailWaveId'])) if (
                old_wave is None and control and control.get('tailWaveId')) else None
            wave = dict(old_wave or self._row(self.wave_key(wave_id), waveId=wave_id,
                predecessorWaveId=(control or {}).get('tailWaveId', ''), state='collecting',
                predecessorFrozenRevision=(predecessor or {}).get('frozenRevision', ''),
                createdAt=self.clock(), openSessions=0, sessionCount=0, memberCount=0,
                intakeEpoch=0, evaluation='', stateEnteredAt=self.clock(), lastProgressAt=self.clock()))
            if wave['state'] == 'ready':
                raise Conflict('Frozen wave cannot admit sessions')
            if wave['state'] != 'collecting':
                wave['stateEnteredAt'] = self.clock()
            wave.update(state='collecting', openSessions=wave['openSessions'] + 1,
                        sessionCount=wave['sessionCount'] + 1, intakeEpoch=wave['intakeEpoch'] + 1,
                        evaluation='', updatedAt=self.clock(), lastProgressAt=self.clock(),
                        visionPendingKey='', waitReason='intake_open')
            row = self._row(key, sessionId=session, waveId=wave_id, declaredCount=declared_count,
                registeredCount=0, resolvedCount=0, state='open', resumedFromSessionId=resumed_from, manifestRevision=uuid.uuid4().hex,
                createdAt=self.clock())
            # Session evidence is separately paged with the member manifest.
            evidence = self._row(f'e!{wave_id}!s!{session}', kind='session', sessionId=session,
                                 state='open', manifestRevision=row['manifestRevision'])
            new_control = dict(control or self._row('control', revision=''))
            new_control.update(collectingWaveId=wave_id, tailWaveId=wave_id)
            # Control fields and the admission itself must commit atomically.
            new_control.update(revision=uuid.uuid4().hex, updatedAt=self.clock())
            self.table.submit_transaction([self._change(control, new_control),
                self._change(old_wave, wave), ('create', row), ('create', evidence)])
            return row
        return self._retry(run)

    def register(self, session, items):
        if not isinstance(items, list) or not 0 < len(items) <= MAX_PAGE:
            raise ValueError('Expected 1-20 manifest items per page')
        if any(not isinstance(item, dict) for item in items):
            raise ValueError('Each manifest item must be an object')
        keys = [identifier(item['itemId']) for item in items]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate itemId in registration page')
        for item in items:
            if not isinstance(item.get('filename'), str) or not 0 < len(item['filename']) <= 1024:
                raise ValueError('Each item requires its intended filename')

        def run():
            control = self._required('control')
            old = self._required(self.session_key(session))
            changes = []
            for item in items:
                key = self.item_key(session, item['itemId'])
                existing = self._get(key)
                if existing:
                    if existing['filename'] != item['filename']:
                        raise Conflict('Item identity cannot be rebound')
                    continue
                changes.append(('create', self._row(key, itemId=item['itemId'],
                    sessionId=session, waveId=old['waveId'], filename=item['filename'], outcome='reserved')))
            if not changes:
                return dict(old)
            if old['state'] != 'open':
                raise Conflict('Session is closed')
            if old['registeredCount'] + len(changes) > old['declaredCount']:
                raise Conflict('Registration exceeds declared parent selection')
            row = dict(old, registeredCount=old['registeredCount'] + len(changes),
                       manifestRevision=uuid.uuid4().hex)
            wave = self._required(self.wave_key(old['waveId']))
            self._commit(control, [self._change(old, row), *changes,
                self._change(wave, dict(wave, lastProgressAt=self.clock(), updatedAt=self.clock()))])
            return row
        return self._retry(run)

    def resolve(self, session, item, *, outcome, photo_id='', processing_revision='',
                filename='', required_steps=()):
        if outcome not in ('accepted', 'duplicate', 'cancelled', 'failed'):
            raise ValueError('Invalid intake outcome')
        if outcome in ('accepted', 'duplicate'):
            if (not all(isinstance(value, str) and 0 < len(value) <= 1024
                        for value in (photo_id, processing_revision, filename))):
                raise ValueError('Accepted items require immutable photo/revision identity')
            if (not isinstance(required_steps, (list, tuple)) or not 0 < len(required_steps) <= 16
                    or any(not isinstance(step, str) or not re.fullmatch(r'[a-z_]{1,32}', step)
                           for step in required_steps)):
                raise ValueError('Accepted items require a processing step policy')
        binding = dict(outcome=outcome, photoId=photo_id, processingRevision=processing_revision,
                       acceptedFilename=filename, requiredSteps=json.dumps(sorted(set(required_steps))))

        def run():
            control = self._required('control')
            old_session = self._required(self.session_key(session))
            old_item = self._required(self.item_key(session, item))
            if old_item['outcome'] != 'reserved':
                if any(old_item.get(k) != v for k, v in binding.items()):
                    raise Conflict('Resolved intake item cannot be rebound')
                return dict(old_session)
            if old_session['state'] != 'open':
                raise Conflict('Session is closed')
            wave_id = old_session['waveId']
            wave = self._required(self.wave_key(wave_id))
            next_wave = dict(wave, lastProgressAt=self.clock(), updatedAt=self.clock())
            changes = [self._change(old_item, dict(old_item, **binding))]
            if outcome in ('accepted', 'duplicate'):
                member_key = f'e!{wave_id}!m!' + digest([photo_id, processing_revision])
                existing = self._get(member_key)
                if existing and existing['requiredSteps'] != binding['requiredSteps']:
                    raise Conflict('Processing policy differs for the same revision')
                if existing is None:
                    member = self._row(member_key, kind='member', waveId=wave_id,
                        photoId=photo_id, processingRevision=processing_revision, filename=filename,
                        requiredSteps=binding['requiredSteps'], outcome='pending', evidenceId='',
                        outstandingLeases=0, evidenceQualified=False, observedAt=0.0)
                    next_wave['memberCount'] += 1
                    changes.append(('create', member))
            row = dict(old_session, resolvedCount=old_session['resolvedCount'] + 1,
                       manifestRevision=uuid.uuid4().hex)
            self._commit(control, [*changes, self._change(old_session, row), self._change(wave, next_wave)])
            return row
        return self._retry(run)

    def close(self, session, manifest_revision, close_id, *, abandoned=False):
        identifier(close_id)

        def run():
            control = self._required('control')
            old = self._required(self.session_key(session))
            if old['state'] != 'open':
                if (old.get('closeId') == close_id and old['manifestRevision'] == manifest_revision
                        and old['state'] == ('abandoned' if abandoned else 'closed')):
                    return dict(old)
                raise Conflict('Session already closed with another request')
            if old['manifestRevision'] != manifest_revision:
                raise Conflict('Close manifest revision is stale')
            if old['registeredCount'] != old['declaredCount'] or old['resolvedCount'] != old['declaredCount']:
                raise Conflict('Declared items or reservations remain unresolved')
            row = dict(old, state='abandoned' if abandoned else 'closed', closeId=close_id,
                       closedAt=self.clock())
            wave = self._required(self.wave_key(old['waveId']))
            open_sessions = wave['openSessions'] - 1
            state = 'collecting' if open_sessions else (
                'waiting_predecessor' if wave['predecessorWaveId'] else 'processing_pending')
            next_wave = dict(wave, openSessions=open_sessions, state=state, evaluation='',
                            waitReason='intake_open' if open_sessions else (
                                'predecessor_incomplete' if wave['predecessorWaveId'] else 'processing_barrier'),
                            lastProgressAt=self.clock(), updatedAt=self.clock())
            if state != wave['state']:
                next_wave['stateEnteredAt'] = self.clock()
            if not open_sessions:
                next_wave['barrierStartedAt'] = self.clock()
            ekey = f"e!{wave['waveId']}!s!{session}"
            prior = self._required(ekey)
            evidence = dict(prior, state=row['state'], manifestRevision=manifest_revision)
            changes = []
            if state == 'processing_pending':
                outbox = self._row(f"shadow-vision!{wave['waveId']}!{wave['intakeEpoch']}",
                    waveId=wave['waveId'], intakeEpoch=wave['intakeEpoch'],
                    destination='vision', dispatchable=False, createdAt=self.clock())
                next_wave['visionPendingKey'] = outbox['RowKey']
                changes.append(('create', outbox))
            self._commit(control, [self._change(old, row), self._change(wave, next_wave),
                                   self._change(prior, evidence), *changes])
            return row
        return self._retry(run)

    def promote_successor(self, wave_id):
        """Advances a wave out of `waiting_predecessor` once its predecessor
        has committed exact indexing-completion evidence. Mirrors exactly
        what close() does for a wave with no predecessor -- the same target
        state and outbox shape -- so there is one processing_pending/
        visionPending transition in this module, not two parallel ones.

        Idempotent and safe to call speculatively: a wave not currently in
        `waiting_predecessor`, or whose predecessor is incomplete, is a
        no-op that returns the wave unchanged (not an error). Callers
        (the opt-in execution layer, or its recovery sweep) can call this on
        every `waiting_predecessor` wave they see without first checking.
        """
        identifier(wave_id)

        def run():
            control = self._required('control')
            wave = self._required(self.wave_key(wave_id))
            if wave['state'] != 'waiting_predecessor':
                return dict(wave)
            predecessor = self._get(self.wave_key(wave['predecessorWaveId']))
            if predecessor is None or predecessor['state'] != 'complete':
                return dict(wave)
            completion = self._get('completion!' + predecessor['waveId'])
            if (not completion or not wave.get('predecessorFrozenRevision')
                    or completion.get('frozenRevision') != wave['predecessorFrozenRevision']
                    or completion.get('frozenRevision') != predecessor.get('frozenRevision')
                    or completion.get('requiredSetId') != predecessor.get('requiredPublicationId')
                    or completion.get('receiptId') != predecessor.get('completionReceiptId')):
                return dict(wave)
            next_wave = dict(wave, state='processing_pending', waitReason='',
                             predecessorSatisfiedAt=self.clock(),
                             stateEnteredAt=self.clock(), lastProgressAt=self.clock(),
                             updatedAt=self.clock())
            outbox = self._row(f"shadow-vision!{wave_id}!{wave['intakeEpoch']}",
                waveId=wave_id, intakeEpoch=wave['intakeEpoch'],
                destination='vision', dispatchable=False, createdAt=self.clock())
            next_wave['visionPendingKey'] = outbox['RowKey']
            self._commit(control, [self._change(wave, next_wave), ('create', outbox)])
            return next_wave
        return self._retry(run)

    def complete_indexing(self, wave_id, ledger):
        """Commit exact publication coverage and predecessor completion together.

        The future index-stage owner must first bind requiredPublicationId and
        enter indexing. Shadow-ready or assignment-only states cannot complete.
        """
        identifier(wave_id)
        proof = ledger.completion_proof()
        receipt_id = digest(proof)
        def run():
            control = self._required('control')
            wave = self._required(self.wave_key(wave_id))
            if (proof['libraryId'] != self.library or proof['waveId'] != wave_id
                    or proof['frozenRevision'] != wave.get('frozenRevision')
                    or proof['requiredSetId'] != wave.get('requiredPublicationId')):
                raise Conflict('Publication coverage does not match the wave frontier')
            if wave['state'] == 'complete' and wave.get('completionReceiptId') == receipt_id:
                return dict(wave)
            if wave['state'] != 'indexing':
                raise Conflict('Only the indexing stage can commit wave completion')
            receipt = self._row('completion!' + wave_id,
                **{key: value for key, value in proof.items() if key != 'libraryId'}, receiptId=receipt_id)
            next_wave = dict(wave, state='complete', completionReceiptId=receipt_id,
                stateEnteredAt=self.clock(), lastProgressAt=self.clock(), updatedAt=self.clock())
            self._commit(control, [self._change(wave, next_wave), ('create', receipt)])
            return next_wave
        return self._retry(run)

    @staticmethod
    def outbox_eligible(wave, outbox):
        if outbox.get('destination') == 'vision':
            return (wave['state'] == 'processing_pending'
                    and wave.get('visionPendingKey') == outbox['RowKey']
                    and wave['intakeEpoch'] == outbox.get('intakeEpoch'))
        if outbox.get('destination') == 'cluster':
            return (wave['state'] == 'ready'
                    and wave.get('frozenRevision') == outbox.get('inputRevision'))
        return False

    def set_dispatchable(self, key, *, dispatchable=True):
        """Flips an existing outbox row's `dispatchable` flag under CAS.

        Used only by the opt-in execution layer (upload_wave_execution.py),
        after it has durably recorded a real WorkStore request for this
        outbox -- never called while a wave is still in pure shadow/
        observation mode. Idempotent: setting the same value twice is a
        no-op that still returns the current row.
        """
        def run():
            control = self._required('control')
            row = self._required(key)
            wave = self._required(self.wave_key(row['waveId']))
            if dispatchable and not self.outbox_eligible(wave, row):
                raise Conflict('Stage release is no longer eligible')
            if bool(row.get('dispatchable')) == bool(dispatchable):
                return dict(row)
            updated = dict(row, dispatchable=bool(dispatchable))
            self._commit(control, [self._change(row, updated)], advance=False)
            return updated
        return self._retry(run)

    def observe(self, wave_id, photo_id, processing_revision, *, outcome, evidence_id,
                outstanding_leases=0, evidence_qualified=False):
        """Internal durable observations; not exposed as a client completion API.

        Pending observations may retract earlier observations before freeze.
        A frozen revision is immutable; corrections require a successor cohort.

        evidence_qualified marks whether the observed revision came from a
        real source-allocated processing generation (storage_utils.
        finalize_uploaded_file's processingRevision stamp) rather than the
        legacy physical-identity/hash proxy -- see upload_wave_shadow.py's
        source_identity. It is purely informational here: it does not change
        settlement/barrier behavior, only what reconcile_page reports.
        """
        identifier(wave_id)
        if outcome not in ('pending', 'settled', 'excluded', 'failed_exhausted', 'deleted'):
            raise ValueError('Invalid processing observation')
        if (not isinstance(evidence_id, str) or not 0 < len(evidence_id) <= 256
                or type(outstanding_leases) is not int or outstanding_leases < 0):
            raise ValueError('Observation requires evidence identity and nonnegative lease count')
        if outcome != 'pending' and outstanding_leases:
            raise Conflict('Unresolved processing lease blocks settlement')

        def run():
            control = self._required('control')
            wave = self._required(self.wave_key(wave_id))
            key = f'e!{wave_id}!m!' + digest([photo_id, processing_revision])
            old = self._required(key)
            if (old['outcome'] == outcome and old['evidenceId'] == evidence_id
                    and old['outstandingLeases'] == outstanding_leases
                    and bool(old.get('evidenceQualified')) == bool(evidence_qualified)):
                return dict(old)
            if wave['state'] == 'ready':
                raise Conflict('Frozen wave evidence cannot change')
            row = dict(old, outcome=outcome, evidenceId=evidence_id,
                       outstandingLeases=outstanding_leases, evidenceQualified=bool(evidence_qualified),
                       observedAt=self.clock())
            self._commit(control, [self._change(old, row),
                self._change(wave, dict(wave, lastProgressAt=self.clock(), updatedAt=self.clock()))])
            return row
        return self._retry(run)

    def evaluate(self, wave_id, *, page_size=MAX_PAGE):
        """Consume one bounded manifest page; restart on concurrent mutations.

        Shadow outboxes are non-dispatchable. No worker/lease/queue release is
        implemented here, including for an all-no-op wave or a predecessor.
        """
        identifier(wave_id)
        if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE:
            raise ValueError('page_size must be between 1 and 20')

        def run():
            control = self._required('control')
            wave = self._required(self.wave_key(wave_id))
            if wave['state'] in ('collecting', 'waiting_predecessor', 'ready'):
                return dict(wave)
            evaluation = json.loads(wave.get('evaluation') or '{}')
            if evaluation.get('revision') != control['revision']:
                evaluation = dict(revision=control['revision'], after='', digest=EMPTY_DIGEST,
                    members=0, sessions=0, pending=0, leases=0, failures=0)
            rows = self._page(f'e!{wave_id}!', evaluation['after'], size=page_size)
            for row in rows:
                evaluation['digest'] = digest([evaluation['digest'], {
                    key: value for key, value in row.items() if key != 'Timestamp'}])
                evaluation['after'] = row['RowKey']
                if row['kind'] == 'session':
                    evaluation['sessions'] += 1
                    evaluation['pending'] += int(row['state'] not in ('closed', 'abandoned'))
                else:
                    evaluation['members'] += 1
                    evaluation['leases'] += row['outstandingLeases']
                    evaluation['pending'] += int(row['outcome'] == 'pending')
                    evaluation['failures'] += int(row['outcome'] == 'failed_exhausted')
            finished = len(rows) < page_size
            next_wave = dict(wave, evaluation=json.dumps(evaluation), updatedAt=self.clock())
            changes = []
            if finished:
                if evaluation['members'] != wave['memberCount'] or evaluation['sessions'] != wave['sessionCount']:
                    raise Conflict('Manifest evidence and cached counts disagree')
                next_wave.update(outstandingMembers=evaluation['pending'], outstandingLeases=evaluation['leases'],
                                 failedMembers=evaluation['failures'], lastEvaluatedAt=self.clock())
                if not evaluation['pending'] and not evaluation['leases']:
                    revision = digest([wave_id, wave['intakeEpoch'], evaluation['digest']])
                    next_wave.update(state='ready', frozenRevision=revision, manifestDigest=evaluation['digest'],
                                     waitReason='', frozenAt=self.clock(), stateEnteredAt=self.clock(),
                                     lastProgressAt=self.clock())
                    outbox = self._row('shadow-cluster!' + wave_id, waveId=wave_id,
                        inputRevision=revision, manifestDigest=evaluation['digest'],
                        destination='cluster', dispatchable=False, createdAt=self.clock())
                    changes.append(('create', outbox))
                    next_control = dict(control, collectingWaveId='', revision=uuid.uuid4().hex,
                                        updatedAt=self.clock())
                    self.table.submit_transaction([self._change(control, next_control),
                        self._change(wave, next_wave), *changes])
                    return next_wave
                next_wave.update(state='processing', evaluation='')
                if wave['state'] != 'processing':
                    next_wave['stateEnteredAt'] = self.clock()
            # Evaluation progress alone does not advance the evidence revision,
            # but CAS on control fences admission/observation during this page.
            self._commit(control, [self._change(wave, next_wave)], advance=False)
            return next_wave
        return self._retry(run)

    def status(self, wave_id):
        wave = dict(self._required(self.wave_key(wave_id)))
        now = self.clock()
        return dict(wave, ageSeconds=max(0, now - wave['createdAt']),
                    stateAgeSeconds=max(0, now - wave['stateEnteredAt']),
                    noProgressSeconds=max(0, now - wave['lastProgressAt']),
                    barrierSeconds=max(0, now - wave['barrierStartedAt']) if 'barrierStartedAt' in wave else 0)
