"""Named connection pool + async send/reap per connection.

Each named connection runs its statements on its own single-thread executor so
`--send` can start a query and then return; a later `--reap` joins it.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

import pymysql


@dataclass
class ConnSpec:
    host: str
    port: int
    user: str
    password: str
    database: str | None = None
    charset: str = "utf8mb4"
    connect_timeout: int = 10
    read_timeout: int = 600
    # A newly created account can be committed on one CN before its metadata
    # reaches the CN selected by a new proxy connection.  This is opt-in per
    # connection so ordinary authentication failures keep failing immediately.
    tenant_visibility_retry_timeout: float = 0
    tenant_visibility_retry_interval: float = 0.5
    tenant_visibility_attempt_timeout: int = 0


@dataclass
class QueryResult:
    ok: bool
    rows: list[tuple] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rowcount: int = 0
    errno: int = 0
    errmsg: str = ""
    sqlstate: str = ""
    # Protocol flags only: obtaining these must not issue a diagnostic SELECT
    # on the subject session (which would change ROW_COUNT/warnings/etc.).
    server_status: int | None = None


class NamedConn:
    def __init__(self, name: str, spec: ConnSpec):
        self.name = name
        self.spec = spec
        self.conn = self._open(spec)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"conn-{name}")
        self.pending: Future | None = None
        self.pending_sql: str = ""
        self.lock = threading.Lock()

    @staticmethod
    def _open(spec: ConnSpec) -> pymysql.connections.Connection:
        kwargs = dict(
            host=spec.host,
            port=spec.port,
            user=spec.user,
            password=spec.password,
            charset=spec.charset,
            connect_timeout=spec.connect_timeout,
            read_timeout=spec.read_timeout,
            autocommit=True,
            local_infile=True,
        )
        deadline = (time.monotonic() + spec.tenant_visibility_retry_timeout
                    if spec.tenant_visibility_retry_timeout > 0 else None)

        def connect():
            attempt = 0
            last_error = None
            while True:
                options = dict(kwargs)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise last_error or pymysql.err.OperationalError(
                            2013, "tenant connection retry budget exhausted")
                    limit = min(spec.tenant_visibility_attempt_timeout or 3, remaining)
                    options["connect_timeout"] = min(spec.connect_timeout, limit)
                    options["read_timeout"] = min(spec.read_timeout, limit)
                try:
                    conn = pymysql.connect(**options)
                    if deadline is not None:
                        # PyMySQL keeps this setting for every later query.
                        # Only authentication gets the short timeout.
                        conn._read_timeout = spec.read_timeout
                    return conn
                except pymysql.err.OperationalError as exc:
                    last_error = exc
                    errno = exc.args[0] if exc.args else None
                    message = str(exc.args[1]).casefold() if len(exc.args) > 1 else ""
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        retryable = (errno == 1045 and "there is no tenant" in message)
                        retryable |= errno == 2013 and "timed out" in message
                        if not retryable or remaining <= 0:
                            raise
                        time.sleep(min(spec.tenant_visibility_retry_interval, remaining))
                    elif attempt < 2 and errno in (2003, 2013):
                        attempt += 1
                        time.sleep(0.5 * attempt)
                    else:
                        raise

        # Only bind a default db if it exists to avoid 1049.
        if spec.database:
            probe = connect()
            try:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise pymysql.err.OperationalError(2013, 'tenant connection retry budget exhausted')
                    probe._read_timeout = min(spec.read_timeout, remaining,
                                              spec.tenant_visibility_attempt_timeout or 3)
                with probe.cursor() as cur:
                    cur.execute("show databases like %s", (spec.database,))
                    if cur.fetchone():
                        kwargs["database"] = spec.database
            finally:
                probe.close()

        return connect()

    def execute_sync(self, sql: str) -> QueryResult:
        return self._execute_blocking(sql)

    def send(self, sql: str) -> None:
        if self.pending is not None:
            raise RuntimeError(
                f"connection {self.name}: another --send is still pending ({self.pending_sql!r})"
            )
        self.pending_sql = sql
        self.pending = self.executor.submit(self._execute_blocking, sql)

    def reap(self, timeout: float | None = None) -> QueryResult:
        if self.pending is None:
            raise RuntimeError(f"connection {self.name}: --reap without --send")
        fut = self.pending
        self.pending = None
        self.pending_sql = ""
        return fut.result(timeout=timeout)

    def has_pending(self) -> bool:
        return self.pending is not None

    def _execute_blocking(self, sql: str) -> QueryResult:
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:
                    cols = [d[0] for d in cur.description]
                    rows = list(cur.fetchall())
                    return QueryResult(ok=True, rows=rows, columns=cols, rowcount=len(rows),
                                       server_status=getattr(self.conn, 'server_status', None))
                return QueryResult(ok=True, rowcount=cur.rowcount,
                                   server_status=getattr(self.conn, 'server_status', None))
        except pymysql.MySQLError as e:
            args = e.args
            errno = args[0] if args else 0
            msg = args[1] if len(args) > 1 else str(e)
            return QueryResult(ok=False, errno=int(errno or 0), errmsg=str(msg))

    def close(self) -> None:
        try:
            if self.pending is not None:
                try:
                    self.pending.result(timeout=5)
                except Exception:
                    pass
        finally:
            try:
                self.executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.executor.shutdown(wait=False)
            try:
                self.conn.close()
            except Exception:
                pass


class ConnectionPool:
    def __init__(self, default_spec: ConnSpec):
        self.default_spec = default_spec
        self.conns: dict[str, NamedConn] = {}
        self.current: str = "default"

    def ensure_default(self) -> None:
        if "default" not in self.conns:
            self.conns["default"] = NamedConn("default", self.default_spec)

    def open(self, name: str, spec: ConnSpec) -> NamedConn:
        if name in self.conns:
            raise RuntimeError(f"connection {name} already exists")
        nc = NamedConn(name, spec)
        self.conns[name] = nc
        return nc

    def switch(self, name: str) -> NamedConn:
        if name not in self.conns:
            raise RuntimeError(f"connection {name} does not exist")
        self.current = name
        return self.conns[name]

    def get(self, name: str | None = None) -> NamedConn:
        n = name or self.current
        if n not in self.conns:
            raise RuntimeError(f"connection {n} does not exist")
        return self.conns[n]

    def disconnect(self, name: str) -> None:
        nc = self.conns.pop(name, None)
        if nc is None:
            raise RuntimeError(f"connection {name} does not exist")
        nc.close()
        if self.current == name:
            self.current = "default" if "default" in self.conns else next(iter(self.conns), "")

    def close_all(self) -> None:
        for nc in list(self.conns.values()):
            nc.close()
        self.conns.clear()
