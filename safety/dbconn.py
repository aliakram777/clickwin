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

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            self._check_open()
            return self._c.execute(sql, params)

    def executemany(self, sql: str, seq: Any) -> sqlite3.Cursor:
        with self.lock:
            self._check_open()
            return self._c.executemany(sql, seq)

    def executescript(self, script: str) -> sqlite3.Cursor:
        with self.lock:
            self._check_open()
            return self._c.executescript(script)

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
            self._c.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                try:
                    self._c.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            self._c.execute("COMMIT")

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
