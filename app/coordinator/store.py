"""Armazenamento de estado do coordenador atrás de uma interface mínima (coleção, chave, dono, expiração).

* MemoryStore — volátil (padrão em desenvolvimento): tudo se perde ao reiniciar.
* SqlStore    — SQLite (arquivo local) ou PostgreSQL (DATABASE_URL), com um único esquema portátil.

Nada é gravado no sistema de arquivos efêmero em produção: use PostgreSQL no Render.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Callable


class Store(ABC):
    kind = "abstract"
    clock: Callable[[], float] = staticmethod(time.time)

    @abstractmethod
    def put(self, coll: str, key: str, value: dict, owner: str | None = None, expires_at: float | None = None) -> None: ...

    @abstractmethod
    def get(self, coll: str, key: str) -> dict | None: ...

    @abstractmethod
    def delete(self, coll: str, key: str) -> bool: ...

    @abstractmethod
    def list(self, coll: str, owner: str | None = None) -> list: ...

    @abstractmethod
    def purge_expired(self) -> int: ...

    @abstractmethod
    def ping(self) -> bool: ...

    def count(self, coll: str, owner: str | None = None) -> int:
        return len(self.list(coll, owner))


class MemoryStore(Store):
    kind = "memory"

    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = threading.RLock()

    def put(self, coll, key, value, owner=None, expires_at=None):
        with self._lock:
            self._data[(coll, key)] = (owner, expires_at, json.dumps(value))

    def _alive(self, entry) -> bool:
        return entry[1] is None or entry[1] >= self.clock()

    def get(self, coll, key):
        with self._lock:
            entry = self._data.get((coll, key))
            return json.loads(entry[2]) if entry and self._alive(entry) else None

    def delete(self, coll, key):
        with self._lock:
            return self._data.pop((coll, key), None) is not None

    def list(self, coll, owner=None):
        with self._lock:
            return [json.loads(e[2]) for (c, _), e in self._data.items()
                    if c == coll and (owner is None or e[0] == owner) and self._alive(e)]

    def purge_expired(self):
        with self._lock:
            dead = [k for k, e in self._data.items() if not self._alive(e)]
            for k in dead:
                del self._data[k]
            return len(dead)

    def ping(self):
        return True


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS kv (coll TEXT NOT NULL, k TEXT NOT NULL, owner_id TEXT, doc TEXT NOT NULL, "
    "expires_at DOUBLE PRECISION, PRIMARY KEY (coll, k))",
    "CREATE INDEX IF NOT EXISTS kv_owner ON kv (coll, owner_id)",
    "CREATE INDEX IF NOT EXISTS kv_expires ON kv (expires_at)",
)


class SqlStore(Store):
    """SQLite (`sqlite:///caminho.db`) ou PostgreSQL (`postgresql://...`). Uma conexão protegida por trava."""

    def __init__(self, url: str):
        self._url = url
        self._pg = url.startswith(("postgres://", "postgresql://"))
        self.kind = "postgresql" if self._pg else "sqlite"
        self._lock = threading.RLock()
        self._conn = None
        self._reconnect_errors: tuple = ()
        self._connect()
        for statement in _SCHEMA:
            self._exec(statement)

    def _connect(self) -> None:
        if self._pg:
            try:
                import psycopg  # dependência opcional: pip install "psycopg[binary]"
            except ImportError as exc:
                raise RuntimeError("PostgreSQL requer o pacote 'psycopg[binary]' (veja requirements.txt).") from exc
            self._reconnect_errors = (psycopg.OperationalError, psycopg.InterfaceError)
            self._conn = psycopg.connect(self._url, autocommit=True, connect_timeout=10)
        else:
            if not self._url.startswith("sqlite:///"):
                raise ValueError("URL SQLite deve ter o formato sqlite:///caminho/arquivo.db")
            self._conn = sqlite3.connect(self._url[len("sqlite:///"):], check_same_thread=False, isolation_level=None)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass

    def _exec(self, sql: str, params: tuple = ()):
        sql = sql.replace("?", "%s") if self._pg else sql
        with self._lock:
            for attempt in (1, 2):
                try:
                    cur = self._conn.execute(sql, params)
                    rows = cur.fetchall() if cur.description else []
                    return rows, cur.rowcount
                except self._reconnect_errors:
                    if attempt == 2:
                        raise
                    self._connect()  # conexões ociosas podem ser encerradas pelo servidor

    def put(self, coll, key, value, owner=None, expires_at=None):
        self._exec("INSERT INTO kv (coll, k, owner_id, doc, expires_at) VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT (coll, k) DO UPDATE SET owner_id = excluded.owner_id, doc = excluded.doc, "
                   "expires_at = excluded.expires_at", (coll, key, owner, json.dumps(value), expires_at))

    def get(self, coll, key):
        rows, _ = self._exec("SELECT doc FROM kv WHERE coll = ? AND k = ? AND (expires_at IS NULL OR expires_at >= ?)",
                             (coll, key, self.clock()))
        return json.loads(rows[0][0]) if rows else None

    def delete(self, coll, key):
        return self._exec("DELETE FROM kv WHERE coll = ? AND k = ?", (coll, key))[1] > 0

    def list(self, coll, owner=None):
        sql, params = "SELECT doc FROM kv WHERE coll = ? AND (expires_at IS NULL OR expires_at >= ?)", [coll, self.clock()]
        if owner is not None:
            sql, params = sql + " AND owner_id = ?", params + [owner]
        return [json.loads(r[0]) for r in self._exec(sql, tuple(params))[0]]

    def purge_expired(self):
        return max(0, self._exec("DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at < ?", (self.clock(),))[1])

    def ping(self):
        try:
            return self._exec("SELECT 1")[0][0][0] == 1
        except Exception:
            return False


def open_store(url: str = "") -> Store:
    return SqlStore(url) if url else MemoryStore()
