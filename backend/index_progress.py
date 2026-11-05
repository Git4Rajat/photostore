"""Task-local evidence of indexing work, never a timer or lease heartbeat."""
from contextlib import contextmanager
from contextvars import ContextVar


_CALLBACK = ContextVar('index_work_progress', default=None)


@contextmanager
def reporting_to(callback):
    token = _CALLBACK.set(callback)
    try:
        yield
    finally:
        _CALLBACK.reset(token)


def completed():
    callback = _CALLBACK.get()
    if callback is not None:
        callback()


def sqlite_progress():
    # SQLite invokes this only after executing VM instructions, including
    # large validation/sort queries. Waiting on a lock does not count.
    completed()
    return 0


class Stream:
    """Report consumed bytes; capture the callback for Azure transfer threads.

    Seeking, polling and zero-byte reads do not prove forward progress.
    The underlying stream remains owned/closed by the caller.
    """
    def __init__(self, stream):
        self.stream = stream
        self.callback = _CALLBACK.get()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, *args):
        data = self.stream.read(*args)
        if data and self.callback is not None:
            self.callback()
        return data

    def readinto(self, buffer):
        count = self.stream.readinto(buffer)
        if count and self.callback is not None:
            self.callback()
        return count

    def write(self, data):
        count = self.stream.write(data)
        if count and self.callback is not None:
            self.callback()
        return count
