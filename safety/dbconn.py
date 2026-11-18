"""İpə-təhlükəsiz SQLite bağlantısı (audit B-5, A-7).

Problem: standart `sqlite3.connect` bağlantısı yaradıcı ipə bağlıdır (başqa ipdən `ProgrammingError`); `check_same_thread=False` ilə paylaşıldıqda isə iki ipin
`BEGIN IMMEDIATE ... COMMIT` əməliyyatları eyni bağlantıda ÇARPAZLAŞIR ("cannot start a transaction within a transaction", itirilmiş COMMIT, sızan rezervlər).

Həll: `LockedConnection` bir `RLock` ilə HƏR sorğunu və açıq tranzaksiyanı seriallaşdırır (`transaction()` kilidi BEGIN-dən COMMIT/ROLLBACK-a qədər saxlayır; başqa ipin
sorğusu açıq tranzaksiyanın içinə düşə bilmir), `check_same_thread=False` ilə açılır və `close()` təkrarlana bilər. API `sqlite3.Connection`-un istifadə olunan hissəsi ilə uyğundur.
Məhdudiyyət: kilid bağlantı daxilindədir (proses daxili); çox-proses yarışı SQLite-ın öz kilidləri və `timeout` ilə həll olunur.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Sequence


class Result:
    """Kilid ALTINDA tam oxunmuş sorğu nəticəsi (audit B-5, cursor yarışı).

    `sqlite3.Cursor` bağlantı ilə bölüşülən canlı ifadədir: başqa ipin `COMMIT/ROLLBACK/execute`-u onu sıfırlaya bilər, `fetchone()`/iterasiya isə kilidsiz çağırıldıqda
    ortaq bağlantıda çarpazlaşa bilər. Buna görə `LockedConnection.execute` bütün sətirləri kilid daxilində oxuyur, cursor-u bağlayır və bu dəyişməz nəticəni qaytarır:
    `fetchone/fetchall/fetchmany`, iterasiya, `rowcount`, `lastrowid`, `description` heç bir canlı ifadəyə toxunmur."""
    __slots__ = ("_rows", "_i", "rowcount", "lastrowid", "description")

    def __init__(self, cur: sqlite3.Cursor) -> None:
        try:
            self._rows = cur.fetchall() if cur.description is not None else []
            self.rowcount, self.lastrowid, self.description = cur.rowcount, cur.lastrowid, cur.description
        finally:
            cur.close()
        self._i = 0

    def fetchone(self) -> Any:
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def fetchmany(self, size: int = 1) -> list[Any]:
        out = self._rows[self._i:self._i + max(0, int(size))]
        self._i += len(out)
        return out

    def fetchall(self) -> list[Any]:
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def __iter__(self) -> "Result":
        return self

    def __next__(self) -> Any:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class LockedConnection:
    def __init__(self, path: str, *, isolation_level: str | None = None, timeout: float = 30, row_factory: Any = None) -> None:
        self._c = sqlite3.connect(path, isolation_level=isolation_level, timeout=timeout, check_same_thread=False)
        if row_factory is not None:
            self._c.row_factory = row_factory
        self.lock = threading.RLock()
        self._closed = False

    # --- sqlite3.Connection-uyğun səth ---
    @property
    def row_factory(self) -> Any:
        return self._c.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        with self.lock:
            self._c.row_factory = value

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> Result:
        with self.lock:
            self._check_open()
            return Result(self._c.execute(sql, params))

    def executemany(self, sql: str, seq: Any) -> Result:
        with self.lock:
            self._check_open()
            return Result(self._c.executemany(sql, seq))

    def executescript(self, script: str) -> Result:
        with self.lock:
            self._check_open()
            return Result(self._c.executescript(script))

    def commit(self) -> None:
        with self.lock:
            self._check_open()
            self._c.commit()

    def rollback(self) -> None:
        with self.lock:
            self._check_open()
            self._c.rollback()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """`BEGIN IMMEDIATE` ... `COMMIT` (xəta olarsa `ROLLBACK`); kilid bütün müddət saxlanır, başqa iplər gözləyir."""
        with self.lock:
            self._check_open()
            self._c.execute("BEGIN IMMEDIATE").close()
            try:
                yield
            except BaseException:
                try:
                    self._c.execute("ROLLBACK").close()
                except sqlite3.Error:
                    pass
                raise
            self._c.execute("COMMIT").close()

    def close(self) -> None:
        with self.lock:
            if not self._closed:
                self._closed = True
                self._c.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")


def connect(path: str, **kw: Any) -> LockedConnection:
    return LockedConnection(path, **kw)
