"""Conservative, case-owned checkpoints; never run SQL on a tested session.

This is a lifecycle recognizer, not a general SQL parser. Unrecognized DDL,
conditional creates, session-local objects and unsafe transaction boundaries
are reported as not checked. It never infers ownership from catalog contents.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re

from .connection import ConnSpec, NamedConn


def audit_table(query, database, table):
    # Keep the observer lazily loaded for interpreters which execute no fixtures.
    from .integrity import audit_table as audit
    return audit(query, database, table)


class _ObserverAccessDenied(RuntimeError):
    """Authorization failure during the initial table lookup."""


def _observer_access_denied(result):
    # MatrixOne uses the generic 20101 for this specific authorization
    # rejection; an unrelated internal error must not trigger fallback.
    return (result.errno in (1044, 1142, 1227) or
            (result.errno == 20101 and result.errmsg.strip().lower() ==
             'internal error: do not have privilege to execute the statement'))


def account_from_login(user):
    """Mirror MO's authenticated-user namespace, not a guessed sys alias.

    Source: MatrixOne 80224b0314c6008ecb4de82d1e37b019d5e355df,
    pkg/frontend/authenticate.go:354-437 (GetTenantInfo/splitUserInput),
    :293 and :533 (GetDefaultTenant/sysAccountName). A connection is already
    authenticated before it contributes any owned table to this tracker.
    """
    user = user.partition('?')[0]
    if ':' in user:
        delimiter = ':'
    elif '#' in user:
        delimiter = '#'
    elif '%3A' in user:
        user, delimiter = user.replace('%3A', ':'), ':'
    else:
        return 'sys' if user else None
    account, _, remainder = user.partition(delimiter)
    username, has_role, role = remainder.partition(delimiter)
    if not account or not username or (has_role and not role):
        return None
    return account.lower()


@dataclass
class Statement:
    kind: str = 'other'
    names: list[tuple[str | None, str]] = field(default_factory=list)
    conditional: bool = False


_LEX = re.compile(
    r"\s+|--(?=\s|$)[^\n]*|\#[^\n]*|/\*.*?\*/|"
    r"`(?:``|[^`])*`|'(?:\\.|''|[^'])*'|\"(?:\\.|\"\"|[^\"])*\"|"
    r"[\w$]+|[^\s]", re.S)


def _tokens(sql):
    tokens = []
    for match in _LEX.finditer(sql):
        token = match.group()
        if token.startswith('/*!'):
            raise ValueError('executable comment')
        if token.isspace() or token.startswith(('--', '#', '/*')):
            continue
        tokens.append(token)
    if ';' in tokens[:-1]:
        raise ValueError('multiple statements')
    return tokens[:-1] if tokens and tokens[-1] == ';' else tokens


def _name(tokens, i):
    def identifier(token):
        if token.startswith('`') and token.endswith('`'):
            return token[1:-1].replace('``', '`')
        if re.fullmatch(r'[\w$]+', token) and not token.isdecimal():
            return token
        raise ValueError('unsupported identifier')
    first = identifier(tokens[i])
    if i + 1 < len(tokens) and tokens[i + 1] == '.':
        return (first, identifier(tokens[i + 2])), i + 3
    return (None, first), i + 1


_IDENTITY_PRESERVING_ALTER_HEADS = frozenset(('ADD', 'DROP', 'MODIFY', 'CHANGE', 'ALTER'))


def _alter_preserves_identity(actions):
    """Only trust known action heads, ignoring commas inside parentheses."""
    depth = 0
    action_start = True
    for token in actions:
        if action_start:
            if token not in _IDENTITY_PRESERVING_ALTER_HEADS:
                return False
            action_start = False
        elif token == '(':
            depth += 1
        elif token == ')':
            if depth == 0:
                return False
            depth -= 1
        elif token == ',' and depth == 0:
            action_start = True
    return depth == 0 and not action_start


def parse_statement(sql):
    """Recognize lifecycle SQL without interpreting strings as identifiers."""
    try:
        tokens = _tokens(sql)
        upper = [token.upper() for token in tokens]
        if not tokens:
            return Statement()
        verb = upper[0]
        if upper[:2] == ['DROP', 'ACCOUNT']:
            i, conditional = 2, False
            if upper[i:i + 2] == ['IF', 'EXISTS']:
                i, conditional = i + 2, True
            token = tokens[i]
            if token.startswith("'") and token.endswith("'") and '\\' not in token:
                name, end = (None, token[1:-1].replace("''", "'")), i + 1
            else:
                name, end = _name(tokens, i)
            if end != len(tokens) or name[0] is not None or not name[1]:
                raise ValueError('unsupported account name')
            return Statement('drop_account', [name], conditional)
        if verb == 'USE':
            name, end = _name(tokens, 1)
            if end != len(tokens) or name[0] is not None:
                raise ValueError('unsupported USE')
            return Statement('use', [name])
        if verb in ('CREATE', 'DROP'):
            i = 1
            if upper[i] == 'INDEX' or upper[i:i + 2] == ['UNIQUE', 'INDEX']:
                return Statement()
            if upper[i] in ('DATABASE', 'SCHEMA', 'TABLE'):
                kind = 'database' if upper[i] in ('DATABASE', 'SCHEMA') else 'table'
                i += 1
            elif upper[i] == 'TEMPORARY' and upper[i + 1] == 'TABLE':
                return Statement('unsupported')
            else:
                return Statement('unsupported' if verb == 'CREATE' else 'other')
            conditional = False
            if upper[i:i + 3] == ['IF', 'NOT', 'EXISTS'] and verb == 'CREATE':
                conditional, i = True, i + 3
            elif upper[i:i + 2] == ['IF', 'EXISTS'] and verb == 'DROP':
                conditional, i = True, i + 2
            names = []
            while True:
                name, i = _name(tokens, i)
                if kind == 'database' and name[0] is not None:
                    raise ValueError('qualified database')
                names.append(name)
                if verb != 'DROP' or kind != 'table' or i >= len(tokens) or tokens[i] != ',':
                    break
                i += 1
            if verb == 'DROP' and upper[i:] not in ([], ['RESTRICT'], ['CASCADE']):
                raise ValueError('unsupported DROP suffix')
            if verb == 'CREATE' and kind == 'table':
                # Ordinary CREATE/LIKE/CTAS only, not special table variants.
                if i >= len(tokens) or upper[i] not in ('(', 'LIKE', 'AS', 'SELECT'):
                    raise ValueError('unsupported table creation')
            return Statement(verb.lower() + '_' + kind, names, conditional)
        if verb == 'RENAME' and upper[1] == 'TABLE':
            names, i = [], 2
            while i < len(tokens):
                old, i = _name(tokens, i)
                if upper[i] != 'TO':
                    raise ValueError('missing TO')
                new, i = _name(tokens, i + 1)
                # MO binds an unqualified destination to the source schema
                # for each pair, not the session's default schema.
                if new[0] is None:
                    new = (old[0], new[1])
                names.extend((old, new))
                if i == len(tokens):
                    break
                if tokens[i] != ',':
                    raise ValueError('invalid rename separator')
                i += 1
            return Statement('rename', names)
        if verb == 'ALTER' and upper[1] == 'TABLE':
            old, i = _name(tokens, 2)
            if upper[i:i + 2] in (['RENAME', 'TO'], ['RENAME', 'AS']):
                new, i = _name(tokens, i + 2)
                if i != len(tokens):
                    return Statement('unsupported', [old])
                # An unqualified ALTER rename destination stays in the
                # source schema, even when the session has no default schema.
                if new[0] is None:
                    new = (old[0], new[1])
                return Statement('rename', [old, new])
            # A later action may rename the table, even when the first
            # action is a known ADD/DROP. Strings and identifiers are
            # already single tokens, so their commas are not separators.
            if _alter_preserves_identity(upper[i:]):
                return Statement()
            return Statement('unsupported', [old])
        if verb in ('PREPARE', 'EXECUTE', 'RESTORE'):
            return Statement('opaque')
        return Statement()
    except (ValueError, IndexError):
        return Statement('unsupported')


@dataclass
class OwnedTable:
    database: str
    table: str
    spec: object
    connection: str
    reasons: set[str] = field(default_factory=set)


class IntegrityTracker:
    def __init__(self, emit, *, branch=None, timeout=20, target='matrixone'):
        self.emit = emit
        self.enabled = branch in (None, '', 'main')
        self.target = target
        self.timeout = max(1, min(30, int(timeout)))
        self.tables = {}
        self.databases = set()
        self.current_database = {}
        self.status = {}
        self.context_changed = set()
        self.disabled_constraints = {}
        self.records = []
        self.failures = []
        self.sidecar_offset = 0
        self.prepared = {}
        self.case_modes = {}

    @staticmethod
    def identity(conn):
        spec = conn.spec
        # This internal key is never serialized: distinct users/tenants/endpoints
        # cannot acquire ownership of each other's tables by matching a name.
        return spec.host, spec.port, spec.user, spec.password

    @staticmethod
    def _catalog_scope(identity):
        host, port, user, password = identity
        account = account_from_login(user)
        return (host, port, 'account', account) if account is not None else identity

    def _same_scope(self, left, right):
        return self._catalog_scope(left) == self._catalog_scope(right)

    def _case_mode(self, conn):
        scope = self._catalog_scope(self.identity(conn))
        if scope in self.case_modes:
            return self.case_modes[scope]
        observer = None
        mode = None
        try:
            spec = replace(conn.spec, database=None, read_timeout=self.timeout,
                           connect_timeout=min(5, self.timeout), tenant_visibility_retry_timeout=0)
            observer = NamedConn('integrity_identifier_policy', spec)
            result = observer.execute_sync('SELECT @@lower_case_table_names')
            if result.ok and len(result.rows) == 1 and len(result.rows[0]) == 1:
                value = result.rows[0][0]
                if not isinstance(value, bool) and str(value) in ('0', '1', '2'):
                    mode = int(value)
        except Exception:
            pass
        finally:
            if observer is not None:
                observer.close()
        self.case_modes[scope] = mode
        if mode is None:
            self._record('not_checked', reason='unknown_identifier_case_policy', phase='tracking')
        return mode

    def _matching_keys(self, conn, name=None, *, database=None):
        db, table = (database, None) if database is not None else self._resolve(conn, name)
        candidates = [key for key in self.tables if self._same_scope(key[0], self.identity(conn))]
        exact = [key for key in candidates if key[1] == db and (table is None or key[2] == table)]
        folded = [key for key in candidates if db is not None and key[1].lower() == db.lower()
                  and (table is None or key[2].lower() == table.lower())]
        # Probe only when case-fold aliases actually exist. Exact normal
        # lifecycle operations add no connection/query overhead.
        if len(folded) == len(exact):
            return exact
        mode = self._case_mode(conn)
        if mode in (1, 2):
            return folded
        if mode is None:
            for key in folded:
                self.tables[key].reasons.add('unknown_identifier_case_policy')
            return []
        return exact

    def _owns_database(self, conn, database):
        candidates = [name for identity, name in self.databases
                      if self._same_scope(identity, self.identity(conn))]
        if database in candidates:
            return True
        if database is not None and any(name.lower() == database.lower() for name in candidates):
            return self._case_mode(conn) in (1, 2)
        return False

    def _resolve(self, conn, name):
        db, table = name
        return db or self.current_database.get(conn.name, conn.spec.database), table

    def connected(self, conn):
        if not isinstance(getattr(conn, 'spec', None), ConnSpec):
            return
        self.current_database[conn.name] = conn.spec.database
        self.status.pop(conn.name, None)
        self.context_changed.discard(conn.name)
        self.disabled_constraints.pop(conn.name, None)
        self.prepared = {key: value for key, value in self.prepared.items() if key[0] != conn.name}

    def _effective_sql(self, conn, sql):
        try:
            tokens = _tokens(sql)
            if len(tokens) >= 2 and tokens[0].upper() == 'EXECUTE':
                return self.prepared.get((conn.name, tokens[1].casefold())) or sql
        except ValueError:
            pass
        return sql

    def _key(self, conn, name):
        return self.identity(conn), *self._resolve(conn, name)

    @staticmethod
    def _belongs_to_account(spec, account):
        return account_from_login(spec.user) == account.lower()

    def _record(self, status, *, database=None, table=None, phase=None, **detail):
        record = dict(status=status, database=database, table=table, phase=phase, **detail)
        self.records.append(record)
        self.emit('integrity_check', **record)
        for reason in detail.get('report', {}).get('exclusions', []):
            self.emit('integrity_exclusion', database=database, table=table, phase=phase, reason=reason)
        if status == 'failed':
            self.failures.append(record)

    def before(self, conn, sql, connections):
        if not isinstance(getattr(conn, 'spec', None), ConnSpec):
            return
        operation = parse_statement(self._effective_sql(conn, sql))
        if operation.kind == 'drop_table':
            keys = {key for name in operation.names for key in self._matching_keys(conn, name)}
            targets = [self.tables[key] for key in sorted(keys, key=lambda key: key[1:])]
        elif operation.kind == 'drop_database':
            db = operation.names[0][1]
            targets = [self.tables[key] for key in self._matching_keys(conn, database=db)]
        elif operation.kind == 'drop_account':
            account = operation.names[0][1]
            targets = [table for table in self.tables.values()
                       if self._belongs_to_account(table.spec, account)]
        else:
            targets = []
        for target in targets:
            # A table may be created by a restricted role and removed by a
            # different, authorized user in the same account. Keep the
            # creator observer first; only if it cannot read table metadata
            # may the cleanup connection supply a second observer. Never use
            # credentials from another account or downgrade an audit failure.
            fallback_spec = (conn.spec if self._same_scope(self.identity(conn), self.identity(target))
                             and self.identity(conn) != self.identity(target) else None)
            self._check(target, connections, 'before_cleanup', fallback_spec=fallback_spec)

    def after(self, conn, sql, result):
        if not isinstance(getattr(conn, 'spec', None), ConnSpec):
            return
        if getattr(result, 'server_status', None) is not None:
            self.status[conn.name] = result.server_status
        if not result.ok:
            return
        try:
            tokens = _tokens(sql)
        except ValueError:
            tokens = []
        if len(tokens) >= 2 and tokens[0].upper() == 'PREPARE':
            prepared = None
            if len(tokens) == 4 and tokens[2].upper() == 'FROM':
                literal = tokens[3]
                # No backslash decoding guesses (NO_BACKSLASH_ESCAPES differs).
                if literal.startswith("'") and literal.endswith("'") and '\\' not in literal:
                    prepared = literal[1:-1].replace("''", "'")
            self.prepared[(conn.name, tokens[1].casefold())] = prepared
            if prepared is None:
                self._record('not_checked', reason='unresolved_prepared_body', phase='tracking')
            return  # PREPARE itself does not execute its DDL/DML.
        sql = self._effective_sql(conn, sql)
        operation = parse_statement(sql)
        kind = operation.kind
        identity = self.identity(conn)
        if kind == 'use':
            self.current_database[conn.name] = operation.names[0][1]
        elif kind == 'create_database' and not operation.conditional:
            self.databases.add((identity, operation.names[0][1]))
        elif kind == 'create_table':
            key = self._key(conn, operation.names[0])
            existing = self._matching_keys(conn, operation.names[0])
            if operation.conditional and existing:
                return  # IF NOT EXISTS must not reset an existing exemption.
            if operation.conditional and not self._owns_database(conn, key[1]):
                self._record('not_checked', database=key[1], table=key[2],
                             reason='conditional_create_not_owned', phase='tracking')
            elif key[1] is None:
                self._record('not_checked', table=key[2], reason='unknown_database', phase='tracking')
            else:
                target = OwnedTable(key[1], key[2], conn.spec, conn.name)
                scope = self._catalog_scope(identity)
                if scope in self.case_modes and self.case_modes[scope] is None:
                    target.reasons.add('unknown_identifier_case_policy')
                if conn.name in self.context_changed:
                    target.reasons.add('session_context_changed')
                if self.disabled_constraints.get(conn.name):
                    target.reasons.add('constraints_intentionally_disabled')
                status = self.status.get(conn.name)
                if isinstance(status, int) and (status & 1 or not status & 2):
                    # Transactional DDL may later roll back the object itself;
                    # do not misreport its absence as an integrity violation.
                    target.reasons.add('transactional_ddl_not_tracked')
                self.tables[key] = target
        elif kind == 'drop_table':
            for name in operation.names:
                for key in self._matching_keys(conn, name):
                    self.tables.pop(key, None)
        elif kind == 'drop_database':
            db = operation.names[0][1]
            for key in self._matching_keys(conn, database=db):
                self.tables.pop(key, None)
            mode = self.case_modes.get(self._catalog_scope(identity))
            self.databases = {item for item in self.databases if not (
                self._same_scope(item[0], identity) and
                (item[1] == db or (mode in (1, 2) and item[1].lower() == db.lower())))}
        elif kind == 'drop_account':
            account = operation.names[0][1]
            self.tables = {key: table for key, table in self.tables.items()
                           if not self._belongs_to_account(table.spec, account)}
            self.databases = {item for item in self.databases
                              if account_from_login(item[0][2]) != account.lower()}
        elif kind == 'rename':
            # RENAME TABLE pairs are applied in order: a later source may be
            # the target of an earlier pair (t1->t2, t2->t3). Resolve each
            # source against the ownership map after the preceding move.
            for old, new in zip(operation.names[::2], operation.names[1::2]):
                for oldkey in self._matching_keys(conn, old):
                    table = self.tables.pop(oldkey)
                    newkey = (oldkey[0], *self._resolve(conn, new))
                    table.database, table.table = newkey[1:]
                    status = self.status.get(conn.name)
                    if isinstance(status, int) and (status & 1 or not status & 2):
                        table.reasons.add('transactional_ddl_not_tracked')
                    self.tables[newkey] = table
        elif kind in ('unsupported', 'opaque'):
            reason = 'opaque_lifecycle_not_tracked' if kind == 'opaque' else 'unsupported_sql_lifecycle'
            self._record('not_checked', reason=reason, phase='tracking')
            # Unobserved DDL may replace/rename a known object; do not later
            # assert that the catalog object still has the same provenance.
            if operation.names:
                affected = {key for name in operation.names for key in self._matching_keys(conn, name)}
            else:
                # Unknown EXECUTE/RESTORE can contain account-wide DDL. Only
                # this case's already-owned targets are exempted; no catalog
                # scan, error-code suppression or missing-index downgrade.
                affected = (key for key in self.tables
                            if kind == 'opaque' or self._same_scope(key[0], identity))
            for key in affected:
                self.tables[key].reasons.add(reason)

        try:
            normalized = ' '.join(_tokens(sql)).lower()
            normalized = re.sub(r'\s*\.\s*', '.', normalized).replace('@ @', '@@')
        except ValueError:
            normalized = ''
        if re.match(r'set\s+(?:default\s+|secondary\s+)?role\b', normalized) or re.match(
                r'set\s+(?:(?:session|local)\s+|@@(?:session\.)?)?'
                r'(sql_mode|time_zone|lower_case_table_names|collation_connection|character_set\w*)\b', normalized):
            self.context_changed.add(conn.name)
            for key, table in self.tables.items():
                if self._same_scope(key[0], identity):
                    table.reasons.add('session_context_changed')
        if re.match(r'(?:grant|revoke)\b|drop\s+(?:user|role)\b', normalized):
            for key, table in self.tables.items():
                if self._same_scope(key[0], identity):
                    table.reasons.add('authorization_context_changed')
        constraints = re.finditer(
            r'\b(foreign_key_checks|check_constraint_checks|unique_checks|mo_pk_check_by_cn)'
            r"\s*=\s*'?(0|off|false|1|on|true)\b", normalized)
        if normalized.startswith('set '):
            disabled = self.disabled_constraints.setdefault(conn.name, set())
            for setting in constraints:
                if setting[2] in ('0', 'off', 'false'):
                    disabled.add(setting[1])
                    for key, table in self.tables.items():
                        if self._same_scope(key[0], identity):
                            table.reasons.add('constraints_intentionally_disabled')
                else:
                    disabled.discard(setting[1])

    def _unsafe_reason(self, target, connections):
        if self.target != 'matrixone':
            return 'unsupported_target'
        if not self.enabled:
            return 'unsupported_branch'
        if target.reasons:
            return sorted(target.reasons)[0]
        if target.connection in self.context_changed:
            return 'session_context_changed'
        for conn in connections:
            if conn.has_pending():
                return 'pending_statement'
            if (self.disabled_constraints.get(conn.name) and
                    self._same_scope(self.identity(conn), self.identity(target))):
                return 'constraints_intentionally_disabled'
            status = self.status.get(conn.name, getattr(getattr(conn, 'conn', None), 'server_status', None))
            if not isinstance(status, int):
                return 'unknown_transaction_state'
            if status & 1 or not status & 2:
                return 'active_transaction'
        return None

    def _check(self, target, connections, phase, *, explicit=False, fallback_spec=None):
        reason = self._unsafe_reason(target, connections)
        if reason:
            self._record('failed' if explicit else 'not_checked', database=target.database,
                         table=target.table, phase=phase, reason=reason)
            return
        from .integrity import IntegrityError, IntegrityQueryError

        def observe(credentials):
            spec = replace(credentials, database=None, read_timeout=self.timeout,
                           connect_timeout=min(5, self.timeout), tenant_visibility_retry_timeout=0)
            observer = NamedConn('integrity_observer', spec)
            first_query = True
            try:
                def query(sql):
                    nonlocal first_query
                    initial = first_query
                    first_query = False
                    result = observer.execute_sync(sql)
                    if not result.ok:
                        if initial and _observer_access_denied(result):
                            raise _ObserverAccessDenied()
                        # Driver messages may contain endpoint/account/row data.
                        raise RuntimeError(f'observer query failed (errno={result.errno})')
                    return result.rows
                return audit_table(query, target.database, target.table)
            finally:
                observer.close()

        try:
            try:
                report = observe(target.spec)
            except IntegrityQueryError as first_error:
                if (fallback_spec is None or first_error.check != 'table' or
                        first_error.source_type is not _ObserverAccessDenied):
                    raise
                report = observe(fallback_spec)
            self._record('checked', database=target.database, table=target.table,
                         phase=phase, report=report)
        except Exception as exc:
            # IntegrityError is explicitly a redacted core contract. Arbitrary
            # driver exceptions must never expose SQL, account or row values.
            detail = {'error': str(exc)} if isinstance(exc, IntegrityError) else {}
            self._record('failed', database=target.database, table=target.table,
                         phase=phase, reason='audit_error', error_type=type(exc).__name__, **detail)

    def explicit(self, conn, argument, connections):
        try:
            tokens = _tokens(argument)
            name, end = _name(tokens, 0)
            if end != len(tokens):
                raise ValueError('expected one table')
            database, table = self._resolve(conn, name)
            if not database:
                raise ValueError('database required')
        except (ValueError, IndexError) as exc:
            raise ValueError('--check_integrity requires one [database.]table identifier') from exc
        existing = self._matching_keys(conn, name)
        scope = self._catalog_scope(self.identity(conn))
        if not existing and scope in self.case_modes and self.case_modes[scope] is None:
            # A failed alias-policy probe must not lose the historic guards
            # just because the automatic matcher correctly declined to guess.
            existing = [key for key in self.tables
                        if self._same_scope(key[0], self.identity(conn))
                        and key[1].lower() == database.lower() and key[2].lower() == table.lower()]
        # Naming one persistent table is an explicit new checkpoint, not an
        # assertion that automatic lifecycle tracking can resume. Use the
        # current caller's credentials rather than a possibly obsolete creator
        # login. Only opaque discovery uncertainty is waived: permissions,
        # changed session semantics and intentional constraint disabling stay
        # guarded, as do pending statements and active transactions below.
        target = OwnedTable(database, table, conn.spec, conn.name)
        for key in existing:
            target.reasons.update(self.tables[key].reasons - {'opaque_lifecycle_not_tracked'})
        if self.disabled_constraints.get(conn.name):
            target.reasons.add('constraints_intentionally_disabled')
        self._check(target, connections, 'explicit', explicit=True)

    def finish(self, connections):
        for target in list(self.tables.values()):
            self._check(target, connections, 'case_end')
        self.emit('integrity_summary', **self.summary())

    def load_reports(self, path):
        """Consume explicit script reports without modifying stdout/goldens."""
        if path is None or not path.exists():
            return
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('integrity evidence exceeds limit')
            with path.open(encoding='utf-8') as stream:
                stream.seek(self.sidecar_offset)
                for line in stream:
                    report = json.loads(line)
                    if not isinstance(report, dict) or not all(
                            isinstance(report.get(key), int) and report[key] >= 0
                            for key in ('checks', 'indexes', 'constraints')):
                        raise ValueError('invalid integrity evidence')
                    if not all(isinstance(report.get(key), str) for key in ('database', 'table')):
                        raise ValueError('invalid integrity target')
                    exclusions = report.get('exclusions', [])
                    if not isinstance(exclusions, list) or not all(isinstance(x, str) for x in exclusions):
                        raise ValueError('invalid integrity exclusions')
                    status = report.get('status', 'checked')
                    if status not in ('checked', 'failed'):
                        raise ValueError('invalid integrity status')
                    details = {}
                    if status == 'failed':
                        error = report.get('error', 'script integrity checkpoint failed')
                        if not isinstance(error, str):
                            raise ValueError('invalid integrity failure')
                        details = dict(reason='audit_error', error=error)
                    self._record(status, database=report['database'], table=report['table'],
                                 phase='script_checkpoint', report=report, **details)
                self.sidecar_offset = stream.tell()
        except Exception as exc:
            self._record('failed', phase='script_checkpoint', reason='invalid_integrity_evidence',
                         error_type=type(exc).__name__)

    def summary(self):
        result = {status: sum(record['status'] == status for record in self.records)
                  for status in ('checked', 'not_checked', 'failed')}
        reports = [record['report'] for record in self.records if 'report' in record]
        result.update({key: sum(report.get(key, 0) for report in reports)
                       for key in ('checks', 'indexes', 'constraints')})
        result['exclusions'] = sum(len(report.get('exclusions', [])) for report in reports)
        return result
