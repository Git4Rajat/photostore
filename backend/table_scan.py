"""Ordered, parallel scan of one Table Storage partition.

A library is one partition, and a full scan is a chain of ~130 sequential page
fetches (~1 s each at 130k photos): the scan is latency-bound, not CPU-bound.
This reads disjoint RowKey ranges at the same time and hands the rows back in
exactly the order a sequential scan would (RowKey ascending), so callers need no
changes.

* Work starts as ~65 one-character ranges (digits, letters, '_' ...). Camera
  libraries are lopsided (almost everything starts with "IMG_"), so a range that
  turns out hot is split on the fly: after ``SPLIT_AFTER_ROWS`` rows its worker
  stops, and the remainder is re-cut on the next character of the key prefix.
  Splitting repeats until the load is spread.
* Memory is bounded: each range has a small queue (``QUEUE_ROWS``); a worker
  that is ahead of the consumer blocks. Ranges are always started in key order,
  so the range the consumer is waiting for is always being worked on (no
  deadlock, even with one worker).
* Any worker error is re-raised to the consumer; abandoning the iterator stops
  the workers.

Tune with ``TABLE_SCAN_PARALLELISM`` (1 disables: plain sequential scan),
``TABLE_SCAN_SPLIT_ROWS`` and ``TABLE_SCAN_QUEUE_ROWS``.
"""
from __future__ import annotations

import collections
import os
import queue
import string
import threading
import time
from typing import Callable, Deque, Dict, Iterator, List, Optional, Sequence

PARALLELISM = int(os.getenv('TABLE_SCAN_PARALLELISM', '4'))
SPLIT_AFTER_ROWS = int(os.getenv('TABLE_SCAN_SPLIT_ROWS', '3000'))
QUEUE_ROWS = int(os.getenv('TABLE_SCAN_QUEUE_ROWS', '1000'))
# Seed boundaries: filenames mostly start with a letter or digit.
_ALPHABET = '-.' + string.digits + string.ascii_uppercase + '_' + string.ascii_lowercase
_SEED_BOUNDARIES = list(_ALPHABET)

_DONE = object()


class _Range:
    """Keys in (lo, hi): ``lo`` inclusive unless ``after`` (then exclusive); ``hi`` exclusive."""

    __slots__ = ('lo', 'after', 'hi', 'rows', 'children', 'finished')

    def __init__(self, lo: Optional[str], hi: Optional[str], after: bool = False) -> None:
        self.lo, self.hi, self.after = lo, hi, after
        self.rows: 'queue.Queue' = queue.Queue(maxsize=QUEUE_ROWS)
        self.children: Optional[List['_Range']] = None
        self.finished = False


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _filter_for(base: str, rng: _Range) -> str:
    parts = [f'({base})'] if base else []
    if rng.lo is not None:
        parts.append(f"RowKey {'gt' if rng.after else 'ge'} {_quote(rng.lo)}")
    if rng.hi is not None:
        parts.append(f'RowKey lt {_quote(rng.hi)}')
    return ' and '.join(parts)


def _cut_chars(first_char: str, last_char: str) -> str:
    """Characters to cut on at one key position: stay inside the character class the
    keys were observed in (digits, lowercase, uppercase), else use the full alphabet."""
    for run in (string.digits, string.ascii_lowercase, string.ascii_uppercase):
        if first_char in run and last_char in run:
            return run
    return _ALPHABET


def _split(first_key: str, last_key: str, hi: Optional[str]) -> List[_Range]:
    """Cut the not-yet-read remainder (last_key, hi) into narrower ranges.

    ``first_key``..``last_key`` is what this range has already produced; they agree on
    a prefix and first differ at position ``p``. The unread keys are spread by cutting
    at ``p`` and a few positions before it (e.g. after "IMG_00000".."IMG_00499" the
    digit places 7, 6 and 5 give cuts IMG_005.., IMG_01.., IMG_1..), so a hot prefix
    fans out across workers in one step instead of one slice at a time."""
    common = len(os.path.commonprefix([first_key, last_key]))
    top = min(common + 1, len(last_key))
    cuts = set()
    for depth in range(top, max(top - 3, 0), -1):
        prefix = last_key[:depth - 1]
        here = last_key[depth - 1]
        other = first_key[depth - 1] if depth - 1 < len(first_key) else here
        for ch in _cut_chars(other, here):
            boundary = prefix + ch
            if boundary > last_key and (hi is None or boundary < hi):
                cuts.add(boundary)
    ranges: List[_Range] = []
    start, after = last_key, True
    for boundary in sorted(cuts):
        ranges.append(_Range(start, boundary, after=after))
        start, after = boundary, False
    ranges.append(_Range(start, hi, after=after))
    return ranges


def scan_partition(
    query_entities: Callable[..., Iterator[Dict]],
    base_filter: str,
    *,
    select: Optional[Sequence[str]] = None,
    workers: Optional[int] = None,
    **query_kwargs,
) -> Iterator[Dict]:
    """Yield the partition's rows (matching ``base_filter``) in RowKey order."""
    workers = PARALLELISM if workers is None else workers
    kwargs = dict(query_kwargs)
    if select:
        kwargs['select'] = list(select)
    if workers <= 1:
        yield from query_entities(base_filter, **kwargs)
        return

    import perf_instrumentation
    started = time.perf_counter()
    stats = {'rows': 0, 'ranges': 0}
    boundaries = _SEED_BOUNDARIES
    seeds = [_Range(None if i == 0 else boundaries[i - 1], boundaries[i] if i < len(boundaries) else None)
             for i in range(len(boundaries) + 1)]
    order: List[_Range] = list(seeds)            # consumption order (children are spliced in)
    pending: Deque[_Range] = collections.deque(seeds)
    lock = threading.Lock()
    wake = threading.Condition(lock)
    stop = threading.Event()
    failure: List[BaseException] = []

    def put(rng: _Range, item) -> bool:
        while not stop.is_set():
            try:
                rng.rows.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def work() -> None:
        while not stop.is_set():
            with wake:
                while not pending and not stop.is_set():
                    if all(r.finished for r in order):
                        return
                    wake.wait(timeout=0.2)
                if stop.is_set():
                    return
                rng = pending.popleft()
                stats['ranges'] += 1
            try:
                count = 0
                next_check = SPLIT_AFTER_ROWS
                first_key = last_key = ''
                for row in query_entities(_filter_for(base_filter, rng), **kwargs):
                    if not put(rng, row):
                        return
                    last_key = str(row.get('RowKey') or last_key)
                    first_key = first_key or last_key
                    count += 1
                    if count >= next_check and last_key:
                        next_check = count + SPLIT_AFTER_ROWS
                        children = _split(first_key, last_key, rng.hi)
                        if len(children) > 1:
                            with wake:
                                rng.children = children
                                at = order.index(rng) + 1
                                order[at:at] = children
                                pending.extendleft(reversed(children))   # next in key order
                                wake.notify_all()
                            break
                put(rng, _DONE)
                with wake:
                    rng.finished = True
                    wake.notify_all()
            except BaseException as exc:  # noqa: BLE001 - relayed to the consumer
                failure.append(exc)
                stop.set()
                with wake:
                    wake.notify_all()
                return

    threads = [threading.Thread(target=work, name=f'table-scan-{i}', daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    try:
        index = 0
        while True:
            with lock:
                if index >= len(order):
                    break
                rng = order[index]
            while True:
                if failure:
                    raise failure[0]
                try:
                    item = rng.rows.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is _DONE:
                    break
                stats['rows'] += 1
                yield item
            index += 1
        if failure:
            raise failure[0]
    finally:
        stop.set()
        with wake:
            wake.notify_all()
        for thread in threads:
            thread.join(timeout=2)
        # rows/ranges/ms tell whether the fan-out helped: ms per 1000 rows should drop vs workers=1.
        perf_instrumentation.log_event(
            'table_scan', workers=workers, rows=stats['rows'], ranges=stats['ranges'],
            ms=round((time.perf_counter() - started) * 1000),
        )
