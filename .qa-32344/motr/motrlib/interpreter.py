"""mysqltest-style interpreter.

Directives supported (a pragmatic subset):

  --source FILE
  --echo TEXT
  --let NAME = VAL
  --let $NAME = `SQL`
  --inc $X
  --dec $X
  --sleep N
  --retry_tenant_connect TIMEOUT [INTERVAL [ATTEMPT_TIMEOUT]]
  --retry_cdc_action ACTION TASK [TIMEOUT] [INTERVAL]
  --wait_cdc_state TASK STATE[,STATE] [TIMEOUT] [INTERVAL]
  --exec CMD
  --system CMD
  --disable_query_log / --enable_query_log
  --disable_result_log / --enable_result_log
  --disable_abort_on_error / --enable_abort_on_error
  --disable_warnings / --enable_warnings   (parsed, not enforced)
  --error CODE[,CODE|ER_NAME]
  --assert_result_contains TEXT  (repeatable; applies to the next statement)
  --assert_result_regex REGEX    (repeatable; applies to the next statement)
  --check_integrity [DATABASE.]TABLE (separate read-only observer checkpoint)
  --sorted_result
  --replace_regex /from/to/ ...
  --replace_column N str[ N str ...]
  --replace_result FROM TO
  --send [SQL]          (on current connection; if SQL inline, send it)
  --reap
  --wait_for_slave      (parsed, no-op)
  connect (name,host,user,pass[,db[,port]])
  connection NAME
  disconnect NAME
  --skip REASON
  --die REASON
  if (COND) { ... }
  while (COND) { ... }
  delimiter X

Variables:
  $VAR expansion is performed on query text and most directive args.
  Conditions in if/while: supports `$x`, `!$x`, `$x = VAL`, `$x != VAL`,
  `$x < N`, `$x > N` (numeric compare if both sides are ints).

Not supported (kept minimal): prepared statements, rpl, query_attributes.
"""
from __future__ import annotations

import os
import math
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .connection import ConnectionPool, ConnSpec, QueryResult
from .errors import MotrError, TestAbort, TestSkip, resolve_error_spec
from .diagnostics import LimitedLineBuffer, safe_text
from .integrity_tracking import IntegrityTracker

VAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

# bare-keyword directives (can appear without `--` prefix)
BARE_DIRECTIVES = ("connect", "connection", "disconnect",
                   "if", "while", "delimiter")
UNUSABLE_CONNECTION_ERRORS = frozenset((0, 2006, 2013, 2055))


def directive_text(stripped: str) -> str | None:
    """If this line is a directive, return the directive body (after `--`);
    otherwise return None."""
    if stripped.startswith("--") and not stripped.startswith("---"):
        return stripped[2:].lstrip()
    for kw in BARE_DIRECTIVES:
        if stripped == kw:
            return stripped
        if re.match(rf"^{kw}\s*\(", stripped) or re.match(rf"^{kw}\b\s", stripped):
            return stripped
    return None


def sql_terminator(text: str, delimiter: str, *,
                   no_backslash_escapes: bool = False,
                   ansi_quotes: bool = False) -> tuple[int | None, bool]:
    """Find a trailing delimiter outside SQL literals and comments.

    Preserve intentional multi-statement COM_QUERY on one logical line.
    The second result protects multiline SQL from mysqltest directives.
    """
    quote = None
    block = False
    last_delimiter = None
    i = 0
    while i < len(text):
        ch = text[i]
        if block:
            if text.startswith("*/", i):
                block = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if (ch == "\\" and not no_backslash_escapes and
                    (quote == "'" or (quote == '"' and not ansi_quotes))):
                i += 2
            elif ch == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                else:
                    quote = None
                    i += 1
            else:
                i += 1
            continue
        if ch.isspace():
            i += 1
        elif ch == "#" or (text.startswith("--", i)
                           and (i + 2 == len(text) or text[i + 2].isspace())):
            newline = text.find("\n", i)
            i = len(text) if newline < 0 else newline + 1
        elif text.startswith("/*", i):
            # Executable comments/hints are not disposable trailing comments.
            if text.startswith(("/*!", "/*+"), i):
                last_delimiter = None
            block = True
            i += 2
        elif delimiter and text.startswith(delimiter, i):
            last_delimiter = i
            i += len(delimiter)
        else:
            last_delimiter = None
            if ch in ("'", '"', chr(96)):
                quote = ch
            i += 1
    open_context = quote is not None or block
    return (None if open_context else last_delimiter), open_context


@dataclass
class Block:
    lines: list[tuple[int, str]]  # (lineno, raw)
    source: Path


@dataclass
class ExecResult:
    stmt: str           # SQL executed (echoed to .result when query log enabled)
    out: list[str]      # rendered output lines (empty for DDL/DML)
    ok: bool            # overall success (includes expected-error mapping)


@dataclass
class Options:
    record: bool = False           # record/regen mode => write .result instead of compare
    verbose: bool = False
    failure_file: Path | None = None  # per-case helper verdict for --exec/--system
    event_sink: object = None
    output_limit_bytes: int = 64 * 1024 * 1024
    integrity_branch: str | None = None
    integrity_timeout: int = 20
    integrity_file: Path | None = None
    integrity_target: str = 'matrixone'


class Interpreter:
    def __init__(self, spec: ConnSpec, include_dirs: list[Path], opts: Options):
        self.spec = spec
        self.include_dirs = include_dirs
        self.opts = opts
        self.pool = ConnectionPool(spec)
        self.workspace_root = Path(__file__).resolve().parent.parent
        std_data_dir = next(
            (path for path in include_dirs if path.name == "std_data"),
            Path("std_data"),
        )
        self.vars: dict[str, str] = {
            "MO_HOST": spec.host,
            "MO_PORT": str(spec.port),
            "MO_USER": spec.user,
            "MO_PASSWORD": spec.password,
            "MO_DATABASE": spec.database or "",
            "MOTR_WORKSPACE": str(self.workspace_root),
            # LOAD DATA LOCAL INFILE is served by the MOTR client process, so
            # this path must point at the runner checkout rather than the CN.
            "std_data": str(std_data_dir.resolve()),
        }
        self.delimiter: str = ";"
        # Cache the lexical mode per connection, but derive it from the live
        # session rather than trying to evaluate SET expressions locally.
        self._no_backslash_escapes: dict[str, bool] = {}
        self._ansi_quotes: dict[str, bool] = {}

        self.query_log = True
        self.result_log = True
        self.abort_on_error = True

        self.expected_errors: list[int] | None = None
        self.pending_replace_regex: list[tuple[re.Pattern, str]] | None = None
        self.pending_replace_column: dict[int, str] | None = None
        self.pending_replace_result: list[tuple[str, str]] | None = None
        self.pending_sorted_result: bool = False
        self.pending_result_contains: list[str] = []
        self.pending_result_regex: list[re.Pattern[str]] = []
        self.next_tenant_visibility_retry: tuple[float, float, int] = (0, 0.5, 0)

        self.output_lines = LimitedLineBuffer(self.opts.output_limit_bytes)

        # Per-case counters, exposed through Interpreter.counts.
        # stmts = SQL statements actually dispatched to the server.
        # ok    = statements that matched expectation (success or an
        #         --error-tolerated failure).
        # fail  = statements that failed unexpectedly (raised TestAbort or
        #         tolerated by --disable_abort_on_error).
        self.counts = {"stmts": 0, "ok": 0, "fail": 0}
        self.first_error = None
        self.operation = {}
        self.async_operations = {}
        self._async_sql = {}
        self.integrity = IntegrityTracker(self._event, branch=opts.integrity_branch,
                                          timeout=opts.integrity_timeout, target=opts.integrity_target)

    def _event(self, kind: str, **fields) -> None:
        if self.opts.event_sink:
            self.opts.event_sink(dict(type=kind, **fields))

    def _first_error(self, errno, message):
        if self.first_error is None:
            self.first_error = dict(self.operation, errno=errno, message=safe_text(message))
            self._event("first_error", **self.first_error)

    # ---------- variable expansion ----------

    def expand(self, s: str) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1)
            return self.vars.get(name, os.environ.get(name, ""))
        # also allow ${VAR}
        s = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                   lambda m: self.vars.get(m.group(1), os.environ.get(m.group(1), "")), s)
        return VAR_RE.sub(repl, s)

    # ---------- lexer: split a file into logical lines ----------

    def _load_file(self, path: Path) -> list[tuple[int, str]]:
        text = path.read_text()
        return [(i + 1, ln) for i, ln in enumerate(text.splitlines())]

    def _leading_skip_reason(self, lines: list[tuple[int, str]]) -> str | None:
        """Return a file-level skip reason before opening the default connection.

        mysqltest cases conventionally put ``--skip`` after their header comments.
        Honour that form before connecting so a disabled regression remains skipped
        even when the configured database is unavailable.  A skip after any
        executable content retains its normal in-order interpreter semantics.
        """
        for _, raw in lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            directive = directive_text(stripped)
            if directive and re.match(r"^skip(?:\s|$)", directive,
                                      flags=re.IGNORECASE):
                return directive[4:].strip()
            return None
        return None

    def _resolve_source(self, arg: str) -> Path:
        p = Path(arg)
        if p.exists():
            return p.resolve()
        for base in self.include_dirs:
            cand = base / arg
            if cand.exists():
                return cand.resolve()
        raise MotrError(f"--source file not found: {arg}")

    # ---------- top-level run ----------

    def run(self, test_file: Path) -> str:
        """Run test_file end-to-end; return rendered output."""
        self.include_dirs.append(test_file.parent)
        try:
            lines = self._load_file(test_file)
            skip_reason = self._leading_skip_reason(lines)
            if skip_reason is not None:
                raise TestSkip(self.expand(skip_reason) or "skipped")
            self.operation = dict(file=str(test_file), line=0, connection="default", sql="CONNECT")
            self._event("connect_start", **self.operation)
            self.pool.ensure_default()
            self._refresh_session_sql_mode("default")
            for conn in self._integrity_connections():
                self.integrity.connected(conn)
            self._event("connect_end", **self.operation)
            self._execute_lines(lines, test_file)
            self.integrity.finish(self._integrity_connections())
            self._assert_integrity()
        finally:
            self.pool.close_all()
            self.include_dirs.pop()
        return "\n".join(self.output_lines) + ("\n" if self.output_lines else "")

    # ---------- main executor ----------

    def _active_connection_name(self) -> str:
        return getattr(self.pool, "current", None) or "default"

    def _refresh_session_sql_mode(self, connection_name: str | None = None) -> None:
        name = connection_name or self._active_connection_name()
        connections = getattr(self.pool, "conns", None)
        if not isinstance(connections, dict) or name not in connections:
            # Lightweight interpreter tests may use pools without live sessions.
            # A real ConnectionPool always exposes conns after opening a session.
            return
        result = self.pool.get(name).execute_sync("SELECT @@SESSION.sql_mode")
        if not result.ok or not result.rows or not result.rows[0]:
            self._no_backslash_escapes.pop(name, None)
            self._ansi_quotes.pop(name, None)
            raise MotrError(
                f"unable to read SESSION sql_mode for connection {name!r}: "
                f"ERROR {result.errno}: {result.errmsg}"
            )
        modes = {
            part.strip().upper()
            for part in str(result.rows[0][0] or "").split(",")
            if part.strip()
        }
        self._no_backslash_escapes[name] = "NO_BACKSLASH_ESCAPES" in modes
        self._ansi_quotes[name] = "ANSI_QUOTES" in modes

    @staticmethod
    def _starts_with_set_statement(sql: str) -> bool:
        """Recognize SET after leading SQL comments without touching literals."""
        i = 0
        size = len(sql)
        while i < size:
            while i < size and sql[i].isspace():
                i += 1
            if sql.startswith("/*", i):
                end = sql.find("*/", i + 2)
                if end < 0:
                    return False
                # Executable comments can contain a real SET. Refreshing for
                # them is safe whether or not the server executes the payload.
                if sql.startswith("/*!", i):
                    payload = sql[i + 3:end]
                    payload = re.sub(r"^\s*\d+\s*", "", payload)
                    return re.match(r"^\s*SET\b", payload,
                                    flags=re.IGNORECASE) is not None
                i = end + 2
                continue
            if sql.startswith("#", i):
                newline = sql.find("\n", i + 1)
                i = size if newline < 0 else newline + 1
                continue
            if (sql.startswith("--", i)
                    and (i + 2 == size or ord(sql[i + 2]) <= 32)):
                newline = sql.find("\n", i + 2)
                i = size if newline < 0 else newline + 1
                continue
            break
        return re.match(r"SET\b", sql[i:], flags=re.IGNORECASE) is not None

    @staticmethod
    def _may_contain_multiple_statements(sql: str, no_backslash_escapes: bool,
                                         ansi_quotes: bool = False) -> bool:
        """Conservatively detect a statement separator outside literals/comments."""
        i = 0
        quote = None
        while i < len(sql):
            ch = sql[i]
            if quote:
                if (ch == "\\" and not no_backslash_escapes and
                        (quote == "'" or (quote == '"' and not ansi_quotes))):
                    i += 2
                elif ch == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 2
                    else:
                        quote = None
                        i += 1
                else:
                    i += 1
            elif ch in ("'", '"', "`"):
                quote = ch
                i += 1
            elif sql.startswith("/*!", i):
                # The server may execute SQL inside a versioned comment.
                return True
            elif sql.startswith("/*", i):
                end = sql.find("*/", i + 2)
                i = len(sql) if end < 0 else end + 2
            elif ch == "#" or (sql.startswith("--", i) and
                               (i + 2 == len(sql) or ord(sql[i + 2]) <= 32)):
                end = sql.find("\n", i)
                i = len(sql) if end < 0 else end + 1
            elif ch == ";":
                return True
            else:
                i += 1
        return False

    def _refresh_sql_mode_after_statement(self, sql: str, result: QueryResult) -> None:
        name = self._active_connection_name()
        if not result.ok and result.errno in UNUSABLE_CONNECTION_ERRORS:
            # Preserve the original SQL result for --error and statement
            # accounting; probing a lost connection would overwrite it.
            self._no_backslash_escapes.pop(name, None)
            self._ansi_quotes.pop(name, None)
            return
        # SET may be a later statement in the same COM_QUERY, and earlier
        # statements may have succeeded even if a later one failed. Read the
        # actual session mode instead of evaluating SET expressions locally.
        mode = self._no_backslash_escapes.get(name, False)
        ansi_quotes = self._ansi_quotes.get(name, False)
        if ((result.ok and self._starts_with_set_statement(sql)) or
                self._may_contain_multiple_statements(sql, mode, ansi_quotes)):
            self._refresh_session_sql_mode()

    def _execute_lines(self, lines: list[tuple[int, str]], source: Path) -> None:
        i = 0
        buf_sql: list[str] = []
        buf_sql_start = 0

        while i < len(lines):
            lineno, raw = lines[i]
            stripped = raw.strip()
            no_backslash_escapes = self._no_backslash_escapes.get(
                self._active_connection_name(), False
            )
            ansi_quotes = self._ansi_quotes.get(self._active_connection_name(), False)
            in_sql_context = bool(buf_sql) and sql_terminator(
                "\n".join(buf_sql), self.delimiter,
                no_backslash_escapes=no_backslash_escapes,
                ansi_quotes=ansi_quotes,
            )[1]

            if not in_sql_context and (not stripped or stripped.startswith("#")):
                i += 1
                continue

            directive = None if in_sql_context else directive_text(stripped)
            if directive is not None:
                # Flush any buffered SQL
                if buf_sql:
                    statement = "\n".join(buf_sql)
                    self._exec_sql(statement, buf_sql_start, source)
                    buf_sql = []
                # Handle block constructs that span multiple lines
                consumed = self._handle_directive(directive, lines, i, source)
                i += consumed
                continue

            # non-directive, non-comment -> SQL
            if not buf_sql:
                buf_sql_start = lineno
            buf_sql.append(raw)
            joined = "\n".join(buf_sql)
            end, _ = sql_terminator(
                joined, self.delimiter,
                no_backslash_escapes=no_backslash_escapes,
                ansi_quotes=ansi_quotes,
            )
            if end is not None:
                statement = joined[:end].rstrip()
                self._exec_sql(statement, buf_sql_start, source)
                buf_sql = []
            i += 1

        if buf_sql:
            joined = "\n".join(buf_sql).rstrip()
            no_backslash_escapes = self._no_backslash_escapes.get(
                self._active_connection_name(), False
            )
            end, _ = sql_terminator(
                joined, self.delimiter,
                no_backslash_escapes=no_backslash_escapes,
                ansi_quotes=self._ansi_quotes.get(self._active_connection_name(), False),
            )
            if end is not None:
                joined = joined[:end].rstrip()
            self._exec_sql(joined, buf_sql_start, source)

    # ---------- directives ----------

    def _handle_directive(self, directive: str, lines: list[tuple[int, str]],
                          i: int, source: Path) -> int:
        """Return number of source lines consumed (>=1)."""
        directive = directive.rstrip()
        # Strip trailing delimiter in directives (common in mysqltest dialect)
        while directive.endswith(self.delimiter):
            directive = directive[: -len(self.delimiter)].rstrip()

        head, _, rest = directive.partition(" ")
        rest = rest.lstrip()

        # multi-line block constructs
        if head in ("if", "while"):
            cond, body, consumed = self._parse_block(directive, lines, i)
            if head == "if":
                if self._eval_cond(cond):
                    self._execute_lines(body, source)
            else:  # while
                while self._eval_cond(cond):
                    self._execute_lines(body, source)
            return consumed

        method = getattr(self, f"_d_{head.replace('-', '_')}", None)
        if method is None:
            # unknown => ignore but warn
            if self.opts.verbose:
                self.output_lines.append(f"# [motr] unknown directive: {head}")
            return 1
        self.directive_line = lines[i][0]
        self.operation = dict(file=str(source), line=self.directive_line,
                              connection=getattr(self.pool, "current", None),
                              sql=f'DIRECTIVE {head}')
        self._event('directive_start', **self.operation)
        method(rest, source)
        self._event('directive_end', **self.operation)
        return 1

    # ----- block parser for if / while -----

    def _parse_block(self, directive: str, lines: list[tuple[int, str]], i: int
                     ) -> tuple[str, list[tuple[int, str]], int]:
        # directive like: "if (cond) {"  or "if (cond)" with "{" on next line
        m = re.match(r"(?:if|while)\s*\((.+)\)\s*(\{)?\s*$", directive)
        if not m:
            raise MotrError(f"bad if/while: {directive}")
        cond = m.group(1).strip()
        has_brace = m.group(2) == "{"

        body: list[tuple[int, str]] = []
        j = i + 1
        if not has_brace:
            # next non-empty line must be "{"
            while j < len(lines) and not lines[j][1].strip():
                j += 1
            if j >= len(lines) or lines[j][1].strip() != "{":
                raise MotrError(f"expected '{{' after {directive}")
            j += 1

        depth = 1
        while j < len(lines):
            ln = lines[j][1]
            s = ln.strip()
            if s == "{":
                depth += 1
                body.append(lines[j])
            elif s == "}":
                depth -= 1
                if depth == 0:
                    return cond, body, (j + 1) - i
                body.append(lines[j])
            else:
                body.append(lines[j])
            j += 1
        raise MotrError(f"unterminated block starting at line {lines[i][0]}")

    def _eval_cond(self, cond: str) -> bool:
        c = self.expand(cond).strip()
        neg = False
        if c.startswith("!"):
            neg = True
            c = c[1:].strip()
        # comparisons
        for op in ("==", "!=", "<=", ">=", "<", ">", "="):
            if op in c:
                lhs, rhs = c.split(op, 1)
                lhs = lhs.strip().strip('"\'')
                rhs = rhs.strip().strip('"\'')
                try:
                    a = int(lhs)
                    b = int(rhs)
                    ok = {"=": a == b, "==": a == b, "!=": a != b,
                          "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
                except ValueError:
                    ok = {"=": lhs == rhs, "==": lhs == rhs, "!=": lhs != rhs,
                          "<": lhs < rhs, "<=": lhs <= rhs,
                          ">": lhs > rhs, ">=": lhs >= rhs}[op]
                return (not ok) if neg else ok
        # truthy: non-empty and not "0"
        truthy = bool(c) and c != "0"
        return (not truthy) if neg else truthy

    # ----- individual directives -----

    def _d_source(self, arg: str, source: Path) -> None:
        p = self._resolve_source(self.expand(arg).strip())
        lines = self._load_file(p)
        self._execute_lines(lines, p)

    def _d_echo(self, arg: str, source: Path) -> None:
        self.output_lines.append(self.expand(arg))

    def _d_sleep(self, arg: str, source: Path) -> None:
        time.sleep(float(self.expand(arg).strip()))

    def _d_real_sleep(self, arg: str, source: Path) -> None:
        time.sleep(float(self.expand(arg).strip()))

    def _d_retry_tenant_connect(self, arg: str, source: Path) -> None:
        """Retry the next ``connect`` only for the cross-CN no-tenant window."""
        tokens = shlex.split(self.expand(arg))
        if not 1 <= len(tokens) <= 3:
            raise MotrError(
                "--retry_tenant_connect requires TIMEOUT [INTERVAL [ATTEMPT_TIMEOUT]]"
            )
        timeout = float(tokens[0])
        interval = float(tokens[1]) if len(tokens) >= 2 else 0.5
        attempt_timeout = int(tokens[2]) if len(tokens) == 3 else 3
        if (not math.isfinite(timeout) or not math.isfinite(interval)
                or timeout <= 0 or interval <= 0 or attempt_timeout <= 0):
            raise MotrError(
                "--retry_tenant_connect arguments must be positive"
            )
        self.next_tenant_visibility_retry = (timeout, interval, attempt_timeout)

    def _d_retry_cdc_action(self, arg: str, source: Path) -> None:
        """Retry PAUSE/RESUME until the catalog reaches the target state.

        ``mo_cdc_task.state`` is updated before the backing daemon task has
        necessarily left Created/ResumeRequested.  The DDL validates the
        daemon status, so the action itself is the readiness probe.  A
        successful DDL response is not sufficient, though: the asynchronous
        transition can still be lost or overwritten while the daemon starts.
        Keep probing the catalog and reissuing the idempotent action until the
        stable target state is visible.
        """
        tokens = shlex.split(self.expand(arg))
        if not 2 <= len(tokens) <= 4:
            raise MotrError(
                "--retry_cdc_action requires ACTION TASK "
                "[TIMEOUT] [INTERVAL]"
            )

        action = tokens[0].casefold()
        task_name = tokens[1]
        if action not in {"pause", "resume"}:
            raise MotrError(
                "--retry_cdc_action ACTION must be pause or resume"
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", task_name):
            raise MotrError(
                "--retry_cdc_action TASK must be an unquoted identifier"
            )

        timeout = float(tokens[2]) if len(tokens) >= 3 else 60.0
        interval = float(tokens[3]) if len(tokens) >= 4 else 0.5
        if timeout <= 0 or interval <= 0:
            raise MotrError(
                "--retry_cdc_action TIMEOUT and INTERVAL must be positive"
            )

        sql = f"{action} cdc task {task_name}"
        target_state = "paused" if action == "pause" else "running"
        escaped_task = task_name.replace("'", "''")
        state_sql = (
            "select state from mo_catalog.mo_cdc_task "
            f"where task_name = '{escaped_task}'"
        )
        if self.query_log:
            self.output_lines.append(sql)
        deadline = time.monotonic() + timeout
        last_observation = "task not found"
        last_action = "not attempted"
        conn = self.pool.get()

        while True:
            state_result = self._observed_query(conn, state_sql, source, self.directive_line)
            if state_result.ok:
                if state_result.rows:
                    state = str(state_result.rows[0][0]).casefold()
                    last_observation = state
                    if state == target_state:
                        return
                else:
                    last_observation = "task not found"
            else:
                last_observation = (
                    f"ERROR {state_result.errno}: {state_result.errmsg}"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.counts["fail"] += 1
                raise TestAbort(
                    f"{sql} did not reach {target_state} within {timeout:g}s "
                    f"(last state: {last_observation}; "
                    f"last action: {last_action})"
                )

            result = self._observed_query(conn, sql, source, self.directive_line)
            self.counts["stmts"] += 1
            if result.ok:
                self.counts["ok"] += 1
                last_action = "accepted"
            else:
                is_transient = (
                    result.errno == 20101
                    and "status can not be change" in result.errmsg.casefold()
                )
                if not is_transient:
                    self.counts["fail"] += 1
                    raise TestAbort(
                        f"{sql} failed: ERROR {result.errno}: {result.errmsg}"
                    )

                self.counts["ok"] += 1
                last_action = f"ERROR {result.errno}: {result.errmsg}"

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval, remaining))

    def _d_wait_cdc_state(self, arg: str, source: Path) -> None:
        """Wait until a CDC task reaches a stable state.

        CDC DDL returns before the asynchronous task state transition has
        necessarily completed. Polling the catalog avoids racing a following
        PAUSE or RESUME against transient Created/Pausing states.
        """
        tokens = shlex.split(self.expand(arg))
        if not 2 <= len(tokens) <= 4:
            raise MotrError(
                "--wait_cdc_state requires TASK STATE[,STATE] "
                "[TIMEOUT] [INTERVAL]"
            )

        task_name = tokens[0]
        expected = {
            state.strip().casefold()
            for state in tokens[1].split(",")
            if state.strip()
        }
        if not expected:
            raise MotrError("--wait_cdc_state requires at least one STATE")

        timeout = float(tokens[2]) if len(tokens) >= 3 else 60.0
        interval = float(tokens[3]) if len(tokens) >= 4 else 0.5
        if timeout <= 0 or interval <= 0:
            raise MotrError(
                "--wait_cdc_state TIMEOUT and INTERVAL must be positive"
            )

        escaped_task = task_name.replace("'", "''")
        sql = (
            "select state from mo_catalog.mo_cdc_task "
            f"where task_name = '{escaped_task}'"
        )
        deadline = time.monotonic() + timeout
        last_observation = "task not found"
        conn = self.pool.get()

        while True:
            result = self._observed_query(conn, sql, source, self.directive_line)
            if result.ok:
                if result.rows:
                    state = str(result.rows[0][0]).casefold()
                    last_observation = state
                    if state in expected:
                        return
                else:
                    last_observation = "task not found"
            else:
                last_observation = (
                    f"ERROR {result.errno}: {result.errmsg}"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                states = ",".join(sorted(expected))
                raise TestAbort(
                    f"CDC task {task_name} did not reach {states} within "
                    f"{timeout:g}s (last: {last_observation})"
                )
            time.sleep(min(interval, remaining))

    def _d_let(self, arg: str, source: Path) -> None:
        # --let $X = VALUE    OR    --let $X = `SELECT ...`
        s = arg.strip()
        if s.startswith("$"):
            s = s[1:]
        name, _, val = s.partition("=")
        name = name.strip()
        val = val.strip()
        if val.startswith("`") and val.endswith("`"):
            q = self.expand(val[1:-1])
            conn = self.pool.get()
            r = self._observed_query(conn, q, source, self.directive_line)
            if not r.ok:
                raise MotrError(f"--let backtick query failed: {r.errmsg}")
            # use first column of first row
            if r.rows:
                self.vars[name] = str(r.rows[0][0]) if r.rows[0] else ""
            else:
                self.vars[name] = ""
        else:
            self.vars[name] = self.expand(val)

    def _d_inc(self, arg: str, source: Path) -> None:
        n = arg.strip().lstrip("$")
        self.vars[n] = str(int(self.vars.get(n, "0")) + 1)

    def _d_dec(self, arg: str, source: Path) -> None:
        n = arg.strip().lstrip("$")
        self.vars[n] = str(int(self.vars.get(n, "0")) - 1)

    def _d_exec(self, arg: str, source: Path) -> None:
        cmd = self.expand(arg)
        if self.query_log:
            self.output_lines.append(f"exec: {cmd}")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           env=self._shell_env())
        if self.result_log and r.stdout:
            self.output_lines.append(r.stdout.rstrip())
        self.integrity.load_reports(self.opts.integrity_file)
        self._assert_no_reported_failures()
        if r.returncode != 0 and self.abort_on_error:
            raise TestAbort(f"exec failed ({r.returncode}): {cmd}\n{r.stderr}")

    def _d_system(self, arg: str, source: Path) -> None:
        # same as exec but without echoing to result by default
        subprocess.run(self.expand(arg), shell=True, env=self._shell_env())
        self.integrity.load_reports(self.opts.integrity_file)
        self._assert_no_reported_failures()

    def _shell_env(self) -> dict[str, str] | None:
        env = os.environ.copy()
        env.update({
            "MO_HOST": self.spec.host,
            "MO_PORT": str(self.spec.port),
            "MO_USER": self.spec.user,
            "MO_PASSWORD": self.spec.password,
            "MO_DATABASE": self.spec.database or "",
            "MOTR_WORKSPACE": str(self.workspace_root),
        })
        include = str(self.workspace_root / "include")
        env["PYTHONPATH"] = include + (os.pathsep + env["PYTHONPATH"]
                                       if env.get("PYTHONPATH") else "")
        if self.opts.failure_file is not None:
            env["MOTR_FAILURE_FILE"] = str(self.opts.failure_file)
        if self.opts.integrity_file is not None:
            env["MOTR_INTEGRITY_FILE"] = str(self.opts.integrity_file)
        return env

    def _assert_no_reported_failures(self) -> None:
        failure_file = self.opts.failure_file
        if failure_file is not None and failure_file.is_file() and failure_file.stat().st_size:
            raise TestAbort("MOTR: scenario helper reported failed assertions")

    def _d_disable_query_log(self, arg: str, source: Path) -> None:
        self.query_log = False

    def _d_enable_query_log(self, arg: str, source: Path) -> None:
        self.query_log = True

    def _d_disable_result_log(self, arg: str, source: Path) -> None:
        self.result_log = False

    def _d_enable_result_log(self, arg: str, source: Path) -> None:
        self.result_log = True

    def _d_disable_abort_on_error(self, arg: str, source: Path) -> None:
        self.abort_on_error = False

    def _d_enable_abort_on_error(self, arg: str, source: Path) -> None:
        self.abort_on_error = True

    def _d_disable_warnings(self, arg: str, source: Path) -> None:
        pass

    def _d_enable_warnings(self, arg: str, source: Path) -> None:
        pass

    def _d_error(self, arg: str, source: Path) -> None:
        self.expected_errors = resolve_error_spec(self.expand(arg).strip())

    def _d_assert_result_contains(self, arg: str, source: Path) -> None:
        expected = self.expand(arg)
        if not expected:
            raise MotrError("--assert_result_contains requires non-empty TEXT")
        self.pending_result_contains.append(expected)

    def _d_assert_result_regex(self, arg: str, source: Path) -> None:
        pattern = self.expand(arg)
        if not pattern:
            raise MotrError("--assert_result_regex requires non-empty REGEX")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise MotrError(f"invalid --assert_result_regex: {exc}") from exc
        self.pending_result_regex.append(compiled)

    def _d_sorted_result(self, arg: str, source: Path) -> None:
        self.pending_sorted_result = True

    def _d_replace_regex(self, arg: str, source: Path) -> None:
        self.pending_replace_regex = self._parse_replace_regex(self.expand(arg))

    def _apply_replace_regex(self, text: str) -> str:
        """Apply pending replace_regex to a single line of text."""
        if not self.pending_replace_regex:
            return text
        for pat, rep in self.pending_replace_regex:
            text = pat.sub(rep, text)
        return text

    @staticmethod
    def _parse_replace_regex(s: str) -> list[tuple[re.Pattern, str]]:
        # Sequence of /from/to/[flags] separated by whitespace.
        out: list[tuple[re.Pattern, str]] = []
        s = s.strip()
        while s:
            if not s.startswith("/"):
                raise MotrError(f"bad --replace_regex: {s}")
            # find 2nd and 3rd unescaped '/'
            i = 1
            parts: list[int] = []
            while i < len(s) and len(parts) < 2:
                if s[i] == "\\":
                    i += 2
                    continue
                if s[i] == "/":
                    parts.append(i)
                i += 1
            if len(parts) < 2:
                raise MotrError(f"bad --replace_regex: {s}")
            frm = s[1:parts[0]]
            to = s[parts[0] + 1:parts[1]]
            # optional flags before whitespace
            k = parts[1] + 1
            flags = 0
            while k < len(s) and s[k].isalpha():
                if s[k] == "i":
                    flags |= re.IGNORECASE
                k += 1
            out.append((re.compile(frm.replace("\\/", "/"), flags), to.replace("\\/", "/")))
            s = s[k:].lstrip()
        return out

    def _d_replace_column(self, arg: str, source: Path) -> None:
        # --replace_column N str [N str ...]
        tokens = shlex.split(self.expand(arg))
        mapping: dict[int, str] = {}
        it = iter(tokens)
        for n in it:
            v = next(it)
            mapping[int(n) - 1] = v  # 1-based -> 0-based
        self.pending_replace_column = mapping

    def _d_replace_result(self, arg: str, source: Path) -> None:
        parts = shlex.split(self.expand(arg))
        if len(parts) % 2 != 0:
            raise MotrError("--replace_result needs pairs")
        self.pending_replace_result = [(parts[i], parts[i + 1]) for i in range(0, len(parts), 2)]

    def _d_send(self, arg: str, source: Path) -> None:
        conn = self.pool.get()
        sql = self.expand(arg).strip()
        if sql.endswith(self.delimiter):
            sql = sql[: -len(self.delimiter)].rstrip()
        if not sql:
            # `--send` with no inline SQL: next SQL will be recorded via --send?
            # Real mysqltest: `--send` alone + following query. We keep inline-only
            # form for simplicity and ask the user to write: `--send SELECT ...`
            raise MotrError("--send requires an inline SQL statement")
        if self.query_log:
            self.output_lines.append(f"{sql}")
        self.async_operations[conn.name] = dict(file=str(source),
            line=getattr(self, 'directive_line', 0), connection=conn.name,
            sql=safe_text(sql))
        self._event('send_start', **self.async_operations[conn.name])
        self.integrity.before(conn, sql, self._integrity_connections())
        conn.send(sql)
        self._async_sql[conn.name] = sql

    def _d_reap(self, arg: str, source: Path) -> None:
        conn = self.pool.get()
        self.operation = self.async_operations.pop(conn.name, dict(
            file=str(source), line=getattr(self, 'directive_line', 0), connection=conn.name, sql='REAP'))
        self._event('reap_start', **self.operation)
        started = time.monotonic()
        original_sql = getattr(conn, 'pending_sql', '')
        r = conn.reap()
        self.operation['cost_ms'] = int((time.monotonic()-started)*1000)
        self._event('reap_end', **self.operation, errno=r.errno)
        self._refresh_sql_mode_after_statement(self._async_sql.pop(conn.name, ""), r)
        self.integrity.after(conn, original_sql, r)
        self._consume_result(conn.name, "(reap)", r)

    def _d_check_integrity(self, arg: str, source: Path) -> None:
        self.integrity.explicit(self.pool.get(), self.expand(arg), self._integrity_connections())

    def _integrity_connections(self):
        connections = getattr(self.pool, 'conns', {})
        return list(connections.values()) if isinstance(connections, dict) else []

    def _assert_integrity(self) -> None:
        if self.integrity.failures:
            self.counts['fail'] += len(self.integrity.failures)
            message = f'integrity checks failed: {len(self.integrity.failures)} checkpoint(s)'
            self._first_error(None, message)
            raise TestAbort(message)

    def _d_skip(self, arg: str, source: Path) -> None:
        self._assert_integrity()
        raise TestSkip(self.expand(arg).strip() or "skipped")

    def _d_die(self, arg: str, source: Path) -> None:
        raise TestAbort(self.expand(arg).strip() or "die")

    def _d_connect(self, arg: str, source: Path) -> None:
        # connect (name, host, user, pass[, db[, port]])
        s = self.expand(arg).strip()
        if not (s.startswith("(") and s.endswith(")")):
            raise MotrError(f"bad connect: {arg}")
        inner = s[1:-1]
        parts = [p.strip() for p in inner.split(",")]
        if len(parts) < 4:
            raise MotrError(f"connect needs name,host,user,pass[,db[,port]]: {arg}")
        name = parts[0]
        host = parts[1] or self.spec.host
        user = parts[2] or self.spec.user
        pw = parts[3]
        db = parts[4] if len(parts) >= 5 else (self.spec.database or "")
        port = int(parts[5]) if len(parts) >= 6 and parts[5] else self.spec.port
        retry_timeout, retry_interval, attempt_timeout = self.next_tenant_visibility_retry
        # The opt-in belongs to precisely one connection attempt.  Do not let
        # a preceding account setup make unrelated authentication retryable.
        self.next_tenant_visibility_retry = (0, 0.5, 0)
        spec = ConnSpec(host=host, port=port, user=user, password=pw,
                        database=db or None, charset=self.spec.charset,
                        connect_timeout=self.spec.connect_timeout,
                        read_timeout=self.spec.read_timeout,
                        tenant_visibility_retry_timeout=retry_timeout,
                        tenant_visibility_retry_interval=retry_interval,
                        tenant_visibility_attempt_timeout=attempt_timeout)
        self.operation = dict(file=str(source), line=getattr(self, "directive_line", 0),
                              connection=name, sql="CONNECT")
        self._event("connect_start", **self.operation)
        self.pool.open(name, spec)
        self.pool.switch(name)
        self._refresh_session_sql_mode(name)
        for connection in self._integrity_connections():
            if getattr(connection, 'name', None) == name:
                self.integrity.connected(connection)
        self._event("connect_end", **self.operation)

    def _d_connection(self, arg: str, source: Path) -> None:
        self.pool.switch(self.expand(arg).strip())

    def _d_disconnect(self, arg: str, source: Path) -> None:
        name = self.expand(arg).strip()
        self.pool.disconnect(name)
        self._no_backslash_escapes.pop(name, None)
        self._ansi_quotes.pop(name, None)

    def _d_delimiter(self, arg: str, source: Path) -> None:
        d = arg.strip()
        if not d:
            raise MotrError("delimiter requires argument")
        self.delimiter = d

    # ---------- SQL execution and result rendering ----------

    def _exec_sql(self, sql: str, lineno: int, source: Path) -> None:
        sql_expanded = self.expand(sql)
        conn = self.pool.get()
        # If this connection has a pending --send, do not overlap.
        if conn.has_pending():
            raise MotrError(
                f"{source.name}:{lineno}: SQL issued while connection "
                f"'{conn.name}' has pending --send (use --reap first)"
            )
        r = self._observed_query(conn, sql_expanded, source, lineno)
        self._refresh_sql_mode_after_statement(sql_expanded, r)
        self._consume_result(conn.name, sql, r)

    def _observed_query(self, conn, sql, source, lineno):
        self.operation = dict(file=str(source), line=lineno, connection=conn.name,
                              sql=safe_text(sql))
        self._event("sql_start", **self.operation)
        started = time.monotonic()
        self.integrity.before(conn, sql, self._integrity_connections())
        r = conn.execute_sync(sql)
        self.integrity.after(conn, sql, r)
        self.operation["cost_ms"] = int((time.monotonic() - started) * 1000)
        self._event("sql_end", **self.operation, errno=r.errno)
        return r

    def _consume_result(self, conn_name: str, sql_for_log: str, r: QueryResult) -> None:
        expected = self.expected_errors
        self.expected_errors = None
        result_contains = self.pending_result_contains
        self.pending_result_contains = []
        result_regex = self.pending_result_regex
        self.pending_result_regex = []

        # Count every dispatched statement exactly once.
        self.counts["stmts"] += 1

        if self.query_log and sql_for_log and sql_for_log != "(reap)":
            self.output_lines.append(sql_for_log)

        if not r.ok:
            if result_contains or result_regex:
                self.counts["fail"] += 1
                self._first_error(
                    r.errno,
                    "result assertion could not be evaluated because the query "
                    f"failed: {sql_for_log}",
                )
                raise TestAbort(
                    "result assertion could not be evaluated because the query "
                    f"failed: {sql_for_log}"
                )
            # A broken connection is not SQL output that can be recorded as a
            # passing golden. Disconnect tests must name the exact error code.
            # errno=0 on a failed result is an unclassified client failure, not
            # the successful outcome represented by --error 0.
            if r.errno in UNUSABLE_CONNECTION_ERRORS and not (
                    r.errno != 0 and expected is not None and r.errno in expected):
                self.counts["fail"] += 1
                self._first_error(r.errno, r.errmsg)
                raise TestAbort(f"connection unusable: ERROR {r.errno}: {r.errmsg}")
            if expected is not None:
                if r.errno in expected:
                    # Expected failure -> ok.
                    self.counts["ok"] += 1
                    if self.result_log:
                        error_msg = f"ERROR {r.errno}: {r.errmsg}"
                        self.output_lines.append(self._apply_replace_regex(error_msg))
                    return
                self.counts["fail"] += 1
                msg = (f"unexpected error {r.errno} (expected {expected}): {r.errmsg}")
                # --disable_abort_on_error applies only to SQL without an
                # explicit expectation. It must not disable assertions.
                self._first_error(r.errno, msg)
                raise TestAbort(msg)
            self.counts["fail"] += 1
            self._first_error(r.errno, r.errmsg)
            if self.abort_on_error:
                raise TestAbort(f"ERROR {r.errno}: {r.errmsg}")
            if self.result_log:
                error_msg = f"ERROR {r.errno}: {r.errmsg}"
                self.output_lines.append(self._apply_replace_regex(error_msg))
            return

        # success
        if expected is not None and 0 not in expected:
            self.counts["fail"] += 1
            msg = f"query succeeded but expected errors {expected}: {sql_for_log}"
            self._first_error(0, msg)
            # An explicit --error is an assertion and must be fatal even when
            # ordinary SQL errors are configured to continue.
            raise TestAbort(msg)

        if result_contains or result_regex:
            rendered_result = "\n".join(
                "\t".join(self._render_cell(cell) for cell in row)
                for row in r.rows
            )
            missing = [
                text for text in result_contains if text not in rendered_result
            ]
            if missing:
                self.counts["fail"] += 1
                formatted = ", ".join(repr(text) for text in missing)
                raise TestAbort(
                    f"result assertion failed for {sql_for_log}: "
                    f"missing {formatted}"
                )
            unmatched = [
                pattern.pattern
                for pattern in result_regex
                if pattern.search(rendered_result) is None
            ]
            if unmatched:
                self.counts["fail"] += 1
                formatted = ", ".join(repr(pattern) for pattern in unmatched)
                raise TestAbort(
                    f"result assertion failed for {sql_for_log}: "
                    f"unmatched regex {formatted}"
                )

        self.counts["ok"] += 1

        if not r.columns:
            return  # DDL/DML: no rowset to print

        if self.result_log:
            self.output_lines.extend(self._render_rowset(r))

    def _render_rowset(self, r: QueryResult) -> list[str]:
        rows = [[self._render_cell(c) for c in row] for row in r.rows]

        # apply replace_column (by index)
        if self.pending_replace_column:
            for row in rows:
                for idx, rep in self.pending_replace_column.items():
                    if idx < len(row):
                        row[idx] = rep
            self.pending_replace_column = None

        if self.pending_sorted_result:
            rows.sort()
            self.pending_sorted_result = False

        lines = ["\t".join(r.columns)]
        for row in rows:
            lines.append("\t".join(row))

        # line-level post-processing
        if self.pending_replace_regex:
            new_lines = []
            for ln in lines:
                for pat, rep in self.pending_replace_regex:
                    ln = pat.sub(rep, ln)
                new_lines.append(ln)
            lines = new_lines
            self.pending_replace_regex = None

        if self.pending_replace_result:
            new_lines = []
            for ln in lines:
                for frm, to in self.pending_replace_result:
                    ln = ln.replace(frm, to)
                new_lines.append(ln)
            lines = new_lines
            self.pending_replace_result = None

        return lines

    @staticmethod
    def _render_cell(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8")
            except UnicodeDecodeError:
                return v.decode("latin-1", errors="replace")
        if isinstance(v, bool):
            return "1" if v else "0"
        return str(v)
