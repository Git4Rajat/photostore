"""Bounded design model, NOT the production wave implementation.

Run with Python's standard library. Explore two ordered waves, one optional
intake reopen, per-member fan-out, three durable handoffs, queue loss, stage
claims, publication receipt/completion, slot release and recovery.

Checks abstract safety plus recoverability (a completion path exists from
every reachable state). This is not a proof of eventual completion under an
unfair scheduler, database atomicity, source fencing, scaling or upload safety.
Mutation modes intentionally remove contracts to demonstrate counterexamples.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import json
import time


@dataclass(frozen=True)
class Wave:
    phase: str = 'collecting'
    closed: bool = False
    generation: int = 0
    members: int = 1
    dispatched: int = 0
    settled: int = 0
    # Per-stage outbox, queue and claim bits: Vision=1, cluster=2, index=4.
    outbox: int = 0
    queued: int = 0
    claimed: int = 0
    # The Vision message captures an intake generation and may become stale.
    vision_message_generation: int = -1
    lost_once: int = 0
    cluster_obligations: bool = True
    allow_reopen: bool = True
    required_receipts: int = 3
    index_receipts: int = 0
    assignment_outcome: str = ''
    receipt_origins: tuple[int, int] = (-1, -1)


@dataclass(frozen=True)
class State:
    waves: tuple[Wave, Wave] = (Wave(), Wave())
    slot: int = -1


def updated(s, i, w, **kw):
    waves = list(s.waves)
    waves[i] = w
    return replace(s, waves=tuple(waves), **kw)


def eligible(s, i, mutant='none'):
    completed = ('complete',) if mutant != 'early_successor' else ('assigned', 'index', 'receipted', 'complete')
    return s.waves[i].closed and (i == 0 or s.waves[i - 1].phase in completed)


def successors(s, mutant):
    """Each yielded edge is one durable step; a crash may occur between edges."""
    for i, w in enumerate(s.waves):
        name = f'w{i}'
        if w.phase == 'collecting' and not w.closed:
            # Initial reservation is resolved; close refers to server acceptance.
            phase = 'vision_pending' if s.slot == i and eligible(replace(
                s, waves=tuple(replace(x, closed=True) if n == i else x
                               for n, x in enumerate(s.waves))), i) else 'waiting'
            yield f'{name}:close intake', updated(s, i, replace(w, closed=True, phase=phase))

        if w.phase == 'waiting' and eligible(s, i, mutant) and s.slot == -1:
            yield f'{name}:reserve stage slot', updated(s, i, replace(w, phase='vision_pending'), slot=i)

        # Reopening is possible once, before the clustering freeze. New work
        # admission invalidates the old message's eligibility, not settled data.
        if i == 0 and w.allow_reopen and w.closed and w.generation == 0 and w.phase in ('vision_pending', 'vision'):
            yield f'{name}:reopen with second member', updated(s, i, replace(
                w, phase='collecting', closed=False, generation=1, members=2,
                claimed=w.claimed if mutant == 'retained_dispatch_claim' else w.claimed & ~1))

        # No-op advancement commits evidence without making a downstream app
        # runnable. Recovery can invoke these same conditional transitions.
        if w.phase == 'vision_pending' and w.members == 0:
            yield f'{name}:skip empty Vision', updated(s, i, replace(w, phase='ready'))
        if w.phase == 'ready' and (not w.cluster_obligations or mutant == 'fabricated_noop'):
            yield f'{name}:skip empty clustering', updated(s, i, replace(
                w, phase='assigned', assignment_outcome='no_work'))
        if w.phase == 'assigned' and w.required_receipts == 0:
            yield f'{name}:skip empty indexing', updated(s, i, replace(w, phase='receipted'))

        stages = (('vision_pending', 1), ('ready', 2), ('assigned', 4))
        for phase, bit in stages:
            if w.phase == phase and not (w.outbox & bit):
                if ((bit == 1 and w.members == 0) or (bit == 2 and not w.cluster_obligations)
                        or (bit == 4 and w.required_receipts == 0)):
                    continue
                if mutant == 'lost_vision_outbox' and bit == 1:
                    # Models a lost producer callback with no source-marker relay.
                    continue
                yield f'{name}:recover outbox {bit}', updated(s, i, replace(w, outbox=w.outbox | bit))

        # Relay retries a sent-but-lost message; dispatch isn't a completion receipt.
        for bit in (1, 2, 4):
            phase_ok = ((bit == 1 and w.phase in ('vision_pending', 'vision'))
                        or (bit == 2 and w.phase in ('ready', 'cluster'))
                        or (bit == 4 and w.phase in ('assigned', 'index')))
            if phase_ok and w.outbox & bit and not ((w.queued | w.claimed) & bit):
                if bit != 1 or eligible(s, i):
                    yield f'{name}:relay {bit}', updated(s, i, replace(
                        w, queued=w.queued | bit,
                        vision_message_generation=w.generation if bit == 1 else w.vision_message_generation))
            if w.queued & bit and not (w.lost_once & bit):
                yield f'{name}:lose queued message {bit}', updated(s, i, replace(
                    w, queued=w.queued & ~bit, lost_once=w.lost_once | bit))

        # A stale queued Vision message may be observed after intake reopens.
        if w.queued & 1:
            valid = (eligible(s, i) and s.slot == i
                     and w.vision_message_generation == w.generation
                     and w.phase in ('vision_pending', 'vision'))
            if valid or mutant == 'stale_claim':
                yield f'{name}:claim Vision', updated(s, i, replace(
                    w, queued=w.queued & ~1, claimed=w.claimed | 1, phase='vision'))
            elif w.phase in ('collecting', 'vision_pending', 'vision'):
                yield f'{name}:reject stale Vision wake', updated(s, i, replace(w, queued=w.queued & ~1))

        # Per-member sends are durable progress, independent of root wake ack.
        if w.phase == 'vision' and w.closed:
            all_members = (1 << w.members) - 1
            for j in range(w.members):
                bit = 1 << j
                if not (w.dispatched & bit):
                    if w.claimed & 1 or mutant != 'lost_fanout':
                        yield f'{name}:dispatch member {j}', updated(s, i, replace(w, dispatched=w.dispatched | bit))
            if w.claimed & 1:
                # Model root ack before full fan-out: recoverable only if ledger
                # records remain a source of dispatch ownership after restart.
                yield f'{name}:ack root wake', updated(s, i, replace(
                    w, claimed=w.claimed & ~1, outbox=w.outbox & ~1))
            if w.settled == all_members:
                yield f'{name}:freeze processing frontier', updated(s, i, replace(
                    w, phase='ready', outbox=w.outbox & ~1, queued=w.queued & ~1, claimed=w.claimed & ~1))

        # Already dispatched source work may finish while intake is reopened.
        if w.phase in ('collecting', 'vision_pending', 'vision'):
            for j in range(w.members):
                bit = 1 << j
                if w.dispatched & bit and not (w.settled & bit):
                    yield f'{name}:settle member {j}', updated(s, i, replace(w, settled=w.settled | bit))

        for bit, waiting, running in ((2, 'ready', 'cluster'), (4, 'assigned', 'index')):
            if w.phase == waiting and w.queued & bit and s.slot == i:
                yield f'{name}:claim stage {bit}', updated(s, i, replace(
                    w, phase=running, queued=w.queued & ~bit, claimed=w.claimed | bit))
            if w.phase == running and w.claimed & bit:
                if bit == 4:
                    for receipt in (1, 2):
                        if w.required_receipts & receipt and not w.index_receipts & receipt:
                            origins = list(w.receipt_origins)
                            origins[0 if receipt == 1 else 1] = i + (mutant == 'foreign_receipt')
                            yield f'{name}:publish projection {receipt}', updated(s, i, replace(
                                w, index_receipts=w.index_receipts | receipt, receipt_origins=tuple(origins)))
                ready = bit == 2 or w.index_receipts == w.required_receipts
                if mutant == 'early_receipt' and bit == 4 and w.index_receipts:
                    ready = True
                if ready:
                    phase = 'assigned' if bit == 2 else 'receipted'
                    yield f'{name}:commit stage {bit}', updated(s, i, replace(
                        w, phase=phase, claimed=w.claimed & ~bit, outbox=w.outbox & ~bit,
                        assignment_outcome='applied' if bit == 2 else w.assignment_outcome))
            if w.phase == running and w.claimed & bit and not (w.lost_once & bit):
                # Crash/lease expiration: source state survives, same work resumes.
                yield f'{name}:expire claim {bit}', updated(s, i, replace(
                    w, phase=waiting, claimed=w.claimed & ~bit, lost_once=w.lost_once | bit))

        if w.phase == 'receipted' and mutant != 'lost_completion':
            yield f'{name}:recover completion', updated(s, i, replace(w, phase='complete'))

        # A deliberately bad implementation releases library ownership after
        # assignment rather than required publication.
        if mutant == 'early_successor' and w.phase == 'assigned' and s.slot == i:
            yield f'{name}:release slot before publication', replace(s, slot=-1)

        if w.phase == 'complete' and s.slot != -1:
            if s.slot == i or mutant == 'stale_release':
                yield f'{name}:release slot', replace(s, slot=-1)


def safety(s):
    for i, w in enumerate(s.waves):
        if w.phase in ('vision_pending', 'vision', 'ready', 'cluster', 'assigned', 'index', 'receipted'):
            assert s.slot == i, f'active w{i} lost library slot'
            assert w.closed, f'w{i} advanced with open intake'
            assert i == 0 or s.waves[i - 1].phase == 'complete', 'successor advanced before predecessor'
        if w.phase in ('ready', 'cluster', 'assigned', 'index', 'receipted', 'complete'):
            assert w.settled == (1 << w.members) - 1, 'clustering before all members settled'
        if w.phase == 'complete':
            assert w.index_receipts == w.required_receipts, 'complete without all publication receipts'
        if w.phase == 'receipted':
            assert w.index_receipts == w.required_receipts, 'publication stage closed before required receipts'
        if w.phase in ('assigned', 'index', 'receipted', 'complete'):
            assert w.assignment_outcome in ('applied', 'no_work'), 'missing assignment outcome'
            if w.assignment_outcome == 'no_work':
                assert not w.cluster_obligations, 'fabricated no-work outcome with assignment obligations'
        for position, bit in enumerate((1, 2)):
            if w.index_receipts & bit:
                assert w.receipt_origins[position] == i, 'foreign wave receipt accepted by scope alone'
        if w.phase == 'vision':
            assert w.members > 0, 'empty processing stage unnecessarily claimed'
        if w.phase == 'cluster':
            assert w.cluster_obligations, 'empty clustering stage unnecessarily claimed'
        if w.phase == 'index':
            assert w.required_receipts, 'empty indexing stage unnecessarily claimed'
        assert w.settled & ~w.dispatched == 0, 'settled member never dispatched'


def done(s):
    return s.slot == -1 and all(w.phase == 'complete' for w in s.waves)


def trace(parent, s):
    path = []
    while parent[s] is not None:
        s, event = parent[s]
        path.append(event)
    return list(reversed(path))


def check(mutant='none', *, profile='normal', limit=750_000):
    started = time.monotonic()
    initial = State()
    if profile == 'no_work':
        initial = State(waves=(Wave(members=0, cluster_obligations=False, required_receipts=0, allow_reopen=False),
                               Wave(members=0, cluster_obligations=False, required_receipts=0, allow_reopen=False)))
    elif profile == 'no_faces_with_cleanup':
        initial = State(waves=(Wave(members=0, cluster_obligations=True, allow_reopen=False), Wave()))
    parent = {initial: None}
    reverse = {}
    queue = deque([initial])
    terminals = set()
    edges = 0
    outgoing = {}
    while queue:
        s = queue.popleft()
        try:
            safety(s)
        except AssertionError as error:
            return dict(mode=mutant, profile=profile, result='safety_counterexample', reason=str(error),
                        states=len(parent), edges=edges, trace=trace(parent, s))
        if done(s):
            terminals.add(s)
        for event, nxt in successors(s, mutant):
            edges += 1
            outgoing[s] = outgoing.get(s, 0) + 1
            reverse.setdefault(nxt, set()).add(s)
            if nxt not in parent:
                parent[nxt] = (s, event)
                queue.append(nxt)
                if len(parent) > limit:
                    raise RuntimeError(f'{mutant}: model state budget exceeded')

    # AG EF done: every reachable state has SOME legal recovery/completion path.
    # Not AF done: a scheduler can choose to retry/reopen or never dispatch forever.
    can_finish = set(terminals)
    queue = deque(terminals)
    while queue:
        for prior in reverse.get(queue.popleft(), ()):
            if prior not in can_finish:
                can_finish.add(prior)
                queue.append(prior)
    stuck = next((s for s in parent if s not in can_finish and not outgoing.get(s)), None)
    if stuck is None:
        stuck = next((s for s in parent if s not in can_finish), None)
    return dict(mode=mutant, profile=profile, result='recoverability_counterexample' if stuck else 'pass',
                states=len(parent), edges=edges, terminal_states=len(terminals),
                unrecoverable_states=len(parent) - len(can_finish),
                trace=trace(parent, stuck) if stuck else [],
                seconds=round(time.monotonic() - started, 3))


if __name__ == '__main__':
    baseline = [check(profile=p) for p in ('normal', 'no_work', 'no_faces_with_cleanup')]
    results = [check(mode) for mode in ('lost_vision_outbox', 'stale_claim',
               'stale_release', 'lost_completion', 'lost_fanout', 'retained_dispatch_claim',
               'early_receipt', 'early_successor', 'fabricated_noop', 'foreign_receipt')]
    print(json.dumps(baseline + results, indent=2))
    assert all(r['result'] == 'pass' for r in baseline), 'baseline design model fails'
    assert all(r['result'] != 'pass' for r in results), 'mutation was not detected'
