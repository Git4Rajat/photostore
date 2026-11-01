import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import index_progress


def test_stream_reports_bytes_in_transfer_thread_but_not_seek_or_eof():
    calls = []
    with index_progress.reporting_to(lambda: calls.append(True)):
        stream = index_progress.Stream(io.BytesIO(b'abc'))
    # Azure can consume a stream from a thread without its caller's context.
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(stream.read, 3).result() == b'abc'
        assert executor.submit(stream.read, 3).result() == b''
    assert calls == [True]
    stream.seek(0)
    assert calls == [True]
    assert stream.readinto(bytearray(3)) == 3
    assert stream.readinto(bytearray(3)) == 0
    assert calls == [True, True]
    stream.write(b'')
    assert calls == [True, True]
    stream.write(b'd')
    assert calls == [True, True, True]


def test_context_is_restored_and_sqlite_reports_executed_work():
    calls = []
    with sqlite3.connect(':memory:') as db:
        db.set_progress_handler(index_progress.sqlite_progress, 100)
        with index_progress.reporting_to(lambda: calls.append(True)):
            db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000) SELECT SUM(x) FROM n').fetchone()
        assert calls
        count = len(calls)
        index_progress.completed()
        assert len(calls) == count
