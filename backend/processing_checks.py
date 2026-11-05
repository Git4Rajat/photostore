"""Attempt-local coalescing of shared checks; visible-write fences stay fresh."""
import threading
import time


class CoalescedCheck:
    def __init__(self, authoritative, *, local=lambda: None, interval=1.0, clock=None):
        self.authoritative, self.local = authoritative, local
        self.interval, self.clock = interval, clock or time.monotonic
        self.lock = threading.Lock()
        self.deadline = 0.0
        self.error = None
        self.calls = self.cached = self.reads = self.forced = self.failures = 0
        self.lock_wait_ms = 0.0

    def __call__(self, *, force=False):
        self.local()  # Heartbeat failure/expiry never waits for the read lock.
        started = self.clock()
        with self.lock:
            self.lock_wait_ms += max(0, self.clock() - started) * 1000
            self.calls += 1
            self.local()  # A blocked reader may have outlived its lease.
            if self.error is not None:
                raise self.error
            if not force and self.clock() < self.deadline:
                self.cached += 1
                return
            self.reads += 1
            self.forced += int(force)
            self.deadline = 0.0
            started = self.clock()
            try:
                self.authoritative()
                self.local()
            except Exception as error:
                self.failures += 1
                self.error = error  # A failed forced read cannot reuse old approval.
                raise
            # Slow I/O does not extend the observation window.
            self.deadline = started + self.interval

    def metrics(self):
        return dict(checks=self.calls, cached_checks=self.cached,
                    authoritative_checks=self.reads, forced_checks=self.forced,
                    failures=self.failures, lock_wait_ms=self.lock_wait_ms)
