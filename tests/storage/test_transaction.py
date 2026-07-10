from src.storage.sql_store import SQLStore


class _FakeCur:
    def execute(self, *a, **k): pass
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeConn:
    def __init__(self): self.commits = 0; self.rollbacks = 0; self.closed = False
    def cursor(self): return _FakeCur()
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def _store(conn):
    s = SQLStore.__new__(SQLStore)
    s._conn = conn
    s._in_txn = False
    return s


def test_transaction_commits_once_for_multiple_cursors():
    conn = _FakeConn(); s = _store(conn)
    with s.transaction():
        with s.cursor() as c: c.execute("A")
        with s.cursor() as c: c.execute("B")
    assert conn.commits == 1        # one commit for the whole block
    assert conn.rollbacks == 0


def test_transaction_rolls_back_all_on_error():
    conn = _FakeConn(); s = _store(conn)
    try:
        with s.transaction():
            with s.cursor() as c: c.execute("A")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conn.commits == 0
    assert conn.rollbacks >= 1


def test_cursor_without_transaction_commits_itself():
    conn = _FakeConn(); s = _store(conn)
    with s.cursor() as c: c.execute("A")
    assert conn.commits == 1
