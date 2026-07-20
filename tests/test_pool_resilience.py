# POOL RESILIENCE — the Render-sleep / Supabase-idle-drop defences.
#
# A pooled PostgreSQL connection can be silently dead (instance slept, pooler
# dropped the session). We verify, with fakes, that checkout:
#   1. probes every connection with SELECT 1 and hands out a live one
#   2. discards dead connections (putconn close=True) and retries
#   3. rebuilds the pool from scratch after a long idle gap (post-sleep)
#   4. survives ALL offered connections being dead (full reset path)
import os, sys, tempfile, time

os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db  # noqa: E402

results = []
def check(name, cond):
    results.append((name, bool(cond)))
    print(("✓ " if cond else "✗ FAIL ") + name)


class FakeCursor:
    def __init__(self, conn): self._conn = conn
    def execute(self, sql):
        if not self._conn.alive:
            raise RuntimeError("server closed the connection unexpectedly")
    def close(self): pass


class FakeConn:
    def __init__(self, name, alive=True):
        self.name, self.alive, self.closed = name, alive, False
    def cursor(self): return FakeCursor(self)
    def rollback(self): pass
    def commit(self): pass
    def close(self): self.closed = True


class FakePool:
    def __init__(self, conns):
        self._conns = list(conns)
        self.discarded = []       # putconn(conn, close=True) lands here
        self.returned = []        # putconn(conn) healthy returns land here
        self.closeall_called = False
    def getconn(self):
        if not self._conns:
            raise RuntimeError("pool exhausted")
        return self._conns.pop(0)
    def putconn(self, conn, close=False):
        (self.discarded if close else self.returned).append(conn)
        if close:
            conn.close()
    def closeall(self):
        self.closeall_called = True
        for c in self._conns:
            c.closed = True


def with_fake_pool(conns, idle_s=0.0):
    """Install a fake pool as if DIALECT were postgres; also freeze activity clock."""
    pool = FakePool(conns)
    db._pool = pool
    db._last_db_activity = time.monotonic() - idle_s
    return pool


orig_dialect, orig_get_pool = db.DIALECT, db._get_pool
db.DIALECT = "postgres"
try:
    # 1. healthy conn passes the probe and is handed out
    healthy = FakeConn("h1")
    pool = with_fake_pool([healthy])
    conn = db.get_db_connection()
    check("1. healthy conn handed out", conn._conn is healthy)
    conn.close()
    check("1. healthy conn returned to pool", healthy in pool.returned)

    # 2. dead first conn is discarded (closed), live second conn used
    dead, live = FakeConn("d1", alive=False), FakeConn("d2", alive=True)
    pool = with_fake_pool([dead, live])
    conn = db.get_db_connection()
    check("2. skipped the dead conn", conn._conn is live)
    check("2. dead conn discarded with close=True", dead in pool.discarded and dead.closed)
    conn.close()

    # 3. long idle gap => pool rebuilt BEFORE checkout (old pool closeall'd)
    fresh = FakeConn("f1")
    rebuilt_pool = FakePool([fresh])
    stale_pool = with_fake_pool([FakeConn("s1")], idle_s=300.0)
    db._get_pool = lambda: rebuilt_pool   # pretend _get_pool rebuilt from scratch
    conn = db.get_db_connection()
    check("3. stale pool was closeall()'d after idle gap", stale_pool.closeall_called)
    check("3. checkout came from the rebuilt pool", conn._conn is fresh)
    conn.close()
    db._get_pool = orig_get_pool

    # 4. short gap (< threshold) => NO rebuild, normal probe path
    near = FakeConn("n1")
    pool = with_fake_pool([near], idle_s=5.0)
    conn = db.get_db_connection()
    check("4. short idle gap does not rebuild", not pool.closeall_called and conn._conn is near)
    conn.close()

    # 5. every offered conn dead => hammer falls to full pool reset + fresh pool
    dead1, dead2, dead3 = (FakeConn(f"x{i}", alive=False) for i in range(3))
    pool = with_fake_pool([dead1, dead2, dead3])
    savior_pool = FakePool([FakeConn("savior", alive=True)])
    # _checkout_pg's final fallback calls _reset_pool() then _get_pool()
    real_reset = db._reset_pool
    def counting_reset():
        real_reset()
        db._pool = savior_pool   # simulate the rebuild producing the savior pool
    db._reset_pool = counting_reset
    conn = db.get_db_connection()
    db._reset_pool = real_reset
    check("5. three dead conns all discarded", all(c.closed for c in (dead1, dead2, dead3)))
    check("5. full-reset fallback returned a working conn", conn._conn.name == "savior")
    conn.close()

    # 6. activity clock is stamped on healthy checkout
    pool = with_fake_pool([FakeConn("t1")], idle_s=0.0)
    before = time.monotonic()
    conn = db.get_db_connection()
    check("6. _last_db_activity refreshed", before <= db._last_db_activity <= time.monotonic())
    conn.close()
finally:
    db.DIALECT, db._get_pool = orig_dialect, orig_get_pool
    db._pool = None
    db._last_db_activity = 0.0

fails = [x for x in results if not x[1]]
print(f"\n================ {len(results)-len(fails)} pass, {len(fails)} fail ================")
sys.exit(1 if fails else 0)
