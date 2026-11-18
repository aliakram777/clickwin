"""B-5 (cursor/transaction yarışı): `LockedConnection` nəticələri kilid altında TAM oxunur; paylaşılan bağlantıda canlı cursor yoxdur."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from safety.dbconn import LockedConnection, Result
from safety.ledger import CostLedger


@pytest.fixture
def conn(tmp_path):
    c = LockedConnection(str(tmp_path / "t.db"))
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    yield c
    c.close()


def test_results_are_materialised_not_live_cursors(tmp_path):
    c = LockedConnection(str(tmp_path / "x.db"))
    c.execute("CREATE TABLE t (a INTEGER)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(5)])
    res = c.execute("SELECT a FROM t ORDER BY a")
    assert isinstance(res, Result) and not isinstance(res, sqlite3.Cursor)
    c.execute("DELETE FROM t")                                    # başqa əməliyyat: əvvəlki nəticəni dəyişə bilməz
    assert [r[0] for r in res] == [0, 1, 2, 3, 4]
    again = c.execute("SELECT a FROM t")
    assert again.fetchall() == []
    kept = c.execute("SELECT 1")
    c.close()
    assert kept.fetchone() == (1,)                                # bağlantı bağlansa da oxunmuş nəticə etibarlıdır (canlı ifadə YOXDUR)
    assert kept.fetchone() is None


def test_result_api_matches_cursor_semantics(conn):
    ins = conn.execute("INSERT INTO t (v) VALUES (?)", ("a",))
    assert ins.rowcount == 1 and ins.lastrowid == 1 and ins.fetchone() is None and ins.fetchall() == []
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("b",), ("c",), ("d",)])
    r = conn.execute("SELECT v FROM t ORDER BY id")
    assert r.fetchone()[0] == "a" and [x[0] for x in r.fetchmany(2)] == ["b", "c"] and [x[0] for x in r.fetchall()] == ["d"] and r.fetchone() is None
    assert conn.execute("UPDATE t SET v = 'z' WHERE id > 2").rowcount == 2
    assert r.description is not None and conn.execute("PRAGMA table_info(t)").fetchall()


def test_reader_never_observes_a_half_finished_or_rolled_back_transaction(conn):
    conn.executemany("INSERT INTO t (v) VALUES (?)", [("base",)] * 300)
    stop, errors, bad = threading.Event(), [], []

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            try:
                with conn.transaction():
                    for _ in range(40):
                        conn.execute("INSERT INTO t (v) VALUES ('w')")
                    if n % 2:
                        raise RuntimeError("geri qaytar")
            except RuntimeError:
                pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            time.sleep(0.002)                                     # RLock ədalətli deyil: oxuyuculara şans ver

    def reader():
        for _ in range(300):
            try:
                rows = [r[0] for r in conn.execute("SELECT v FROM t")]
                w = rows.count("w")
                if w % 40:
                    bad.append(w)                                 # tranzaksiyanın yarısı görünüb
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
    wt = threading.Thread(target=writer)
    wt.start()
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda _: reader(), range(4)))
    stop.set()
    wt.join(10)
    assert errors == [] and bad == []
    remaining = conn.execute("SELECT COUNT(*) FROM t WHERE v = 'w'").fetchone()[0]
    assert remaining % 40 == 0                                    # yalnız tam təsdiqlənmiş tranzaksiyalar qalıb (geri qaytarılanlar yox)


def test_concurrent_transactions_do_not_nest_or_lose_commits(conn):
    errors = []

    def work(i):
        try:
            for j in range(15):
                with conn.transaction():
                    conn.execute("INSERT INTO t (v) VALUES (?)", (f"{i}-{j}-a",))
                    time.sleep(0.0005)
                    conn.execute("INSERT INTO t (v) VALUES (?)", (f"{i}-{j}-b",))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(work, range(8)))
    assert errors == []
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 8 * 15 * 2
    ids = [r[0] for r in conn.execute("SELECT id FROM t ORDER BY id")]
    pairs = [conn.execute("SELECT v FROM t WHERE id IN (?, ?) ORDER BY id", (ids[k], ids[k + 1])).fetchall() for k in range(0, len(ids), 2)]
    assert all(p[0][0][:-1] == p[1][0][:-1] for p in pairs)      # hər tranzaksiyanın iki sətri ardıcıl (çarpazlaşma yox)


def test_lastrowid_and_rowcount_belong_to_the_calling_threads_statement(conn):
    seen, errors = {}, []

    def work(i):
        try:
            for j in range(40):
                res = conn.execute("INSERT INTO t (v) VALUES (?)", (f"{i}:{j}",))
                row = conn.execute("SELECT v FROM t WHERE id = ?", (res.lastrowid,)).fetchone()
                if row[0] != f"{i}:{j}" or res.rowcount != 1:
                    errors.append((i, j, row[0], res.rowcount))
                seen[(i, j)] = res.lastrowid
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    with ThreadPoolExecutor(10) as ex:
        list(ex.map(work, range(10)))
    assert errors == [] and len(set(seen.values())) == 400


def test_commit_and_rollback_are_serialised_with_reads(tmp_path):
    c = LockedConnection(str(tmp_path / "x.db"), isolation_level="")      # Registry üslubu: Python tranzaksiyaları
    c.execute("CREATE TABLE t (a INTEGER)")
    c.commit()
    errors = []

    def work(i):
        try:
            for j in range(40):
                c.execute("INSERT INTO t VALUES (?)", (i,))
                if j % 2:
                    c.commit()
                else:
                    c.rollback()
                c.execute("SELECT COUNT(*) FROM t").fetchone()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(work, range(6)))
    assert errors == []
    c.close()


def test_ledger_reads_during_concurrent_reserve_commit_release_stay_consistent(tmp_path):
    led = CostLedger(tmp_path / "l.db")
    errors, stop = [], threading.Event()

    def worker(t):
        try:
            for i in range(60):
                op = f"op-{t}-{i}"
                if led.reserve(op, f"a{t}", 0.05, global_limit=5.0).ok:
                    (led.commit(op, 0.04) if i % 2 else led.release(op))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        while not stop.is_set():
            try:
                total = led.total()
                if total > 5.0 + 1e-6 or any(e.amount < 0 for e in led.entries()):
                    errors.append(("inconsistent", total))
                led.by_agent()
                led.unknown_entries()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
    rt = threading.Thread(target=reader)
    rt.start()
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(worker, range(6)))
    stop.set()
    rt.join(10)
    assert errors == []
    led.close()
