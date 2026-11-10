"""Shared, read-only MatrixOne table integrity audit.

``query(sql)`` must run SELECTs on one existing, tenant-scoped connection and
return rows as tuples. The caller owns deadlines, transaction and connection.
Run at a quiescent DDL boundary: metadata is checked again at exit, but this is
not a DDL lock or a global snapshot. Each physical comparison is ONE statement,
so its two sides share a statement snapshot even under read committed.

This is an additional storage/constraint oracle, not a replacement for literal
expected rows. It cannot detect a value lost identically from base and index,
nor prove rejected-DML atomicity or that enforcement rejects future writes.

Encoding scope follows MO 80224b0314c6008ecb4de82d1e37b019d5e355df
pkg/sql/compile/util.go (genInsertIndexTableSql): raw single UNIQUE keys,
serial() composite UNIQUE/PK, serial_full() ordinary keys including the PK.
Partitioned index layouts, native Unicode opaque keys, serialized ENUM
keys/ENUM primary targets, prefixes/expressions, covering and specialized indexes are explicitly excluded;
never infer their correctness from counts. Raw single-column UNIQUE ENUM with
a non-ENUM primary key remains supported.
"""
from __future__ import annotations

from collections import defaultdict
import re


class IntegrityError(AssertionError):
    """Violation or inability to perform an advertised integrity check."""


def _ident(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        raise IntegrityError('invalid integrity identifier')
    return '`' + value.replace('`', '``') + '`'


def _literal(value):
    # Independent of NO_BACKSLASH_ESCAPES and ANSI_QUOTES session settings.
    return "CONVERT(X'" + value.encode('utf-8').hex() + "' USING utf8mb4)"


def _tag(value):
    return re.sub(r'[^A-Za-z0-9_:.-]', '_', value)


class IntegrityQueryError(IntegrityError):
    """Keep the failed check and exception type, not server-supplied text."""

    def __init__(self, check, source_type):
        self.check = _tag(check)
        self.source_type = source_type
        super().__init__(f'integrity query failed: {self.check} ({source_type.__name__})')


def _index_hint_name(name, kind, algo):
    if name != '':
        return _ident(name)
    # Inline FULLTEXT can be unnamed in the catalog, but it cannot be named
    # in IGNORE INDEX. Do not make empty names a generic exclusion.
    algo = str(algo or '').strip().lower()
    if ((kind == 'FULLTEXT' and not algo) or
            (kind in ('FULLTEXT', 'MULTIPLE') and algo in ('fulltext', 'fulltext2'))):
        return None
    if kind in ('PRIMARY', 'UNIQUE', 'MULTIPLE') and algo in ('', 'btree'):
        raise IntegrityError('unnamed ordinary index metadata')
    raise IntegrityError('unnamed index metadata is not a known fulltext index')


def _ignore_index_hints(indexes):
    names = {_index_hint_name(*row) for row in indexes} - {None}
    return ' IGNORE INDEX (' + ','.join(sorted(names)) + ')' if names else ''


def _same_name(left, right):
    """Mirror MO identifier lookup without conflating case-sensitive schemas."""
    return ('(' + left + '=' + right + ' OR (@@lower_case_table_names<>0 AND LOWER(' +
            left + ')=LOWER(' + right + ')))')


def audit_table(query, database, table):
    """Return counts/details/exclusions, or raise a redacted IntegrityError.

    ``indexes`` counts fully compared physical B-tree indexes, ``constraints``
    counts validated PK/UK/NOT NULL/CHECK/FK invariants. ``exclusions`` is always
    explicit; an empty list is only complete within the documented scope.
    The database/table are separate raw identifiers, never SQL fragments.
    """
    target = _ident(database) + '.' + _ident(table)
    report = dict(database=database, table=table, checks=0, indexes=0,
                  constraints=0, exclusions=[], details=[])

    def rows(label, sql, width=None):
        try:
            result = list(query('/*integrity:' + _tag(label) + '*/ ' + sql))
        except Exception as exc:
            # DB errors may include values, SQL and credentials: retain only
            # the safe check label and exception class, never its message.
            raise IntegrityQueryError(label, type(exc)) from None
        if width is not None and any(len(row) != width for row in result):
            raise IntegrityError('invalid metadata/result shape: ' + _tag(label))
        return result

    def metrics(label, sql, width):
        result = rows(label, sql, width)
        if len(result) != 1:
            raise IntegrityError('missing aggregate result: ' + _tag(label))
        values = []
        for value in result[0]:
            if value is None or isinstance(value, bool):
                raise IntegrityError('invalid aggregate: ' + _tag(label))
            try:
                number = int(value)
            except (ValueError, TypeError, OverflowError):
                raise IntegrityError('invalid aggregate: ' + _tag(label)) from None
            if number < 0 or number != value:
                raise IntegrityError('invalid aggregate: ' + _tag(label))
            values.append(number)
        report['checks'] += 1
        report['details'].append({'check': label, 'counts': values})
        return values

    scope = ('t.account_id=current_account_id() AND ' + _same_name('t.reldatabase', _literal(database)) +
             ' AND ' + _same_name('t.relname', _literal(table)))
    metadata_sql = {
        'table': 'SELECT t.rel_id,t.relkind,t.partitioned FROM mo_catalog.mo_tables t WHERE ' + scope,
        'columns': ('SELECT c.attname,c.attnotnull,c.att_is_hidden,s.DATA_TYPE,s.COLLATION_NAME,s.EXTRA '
                    'FROM mo_catalog.mo_columns c JOIN mo_catalog.mo_tables t '
                    'ON c.account_id=t.account_id AND c.att_relname_id=t.rel_id '
                    'LEFT JOIN information_schema.COLUMNS s ON s.TABLE_SCHEMA=t.reldatabase '
                    'AND s.TABLE_NAME=t.relname AND s.COLUMN_NAME=c.attname '
                    'WHERE ' + scope + ' ORDER BY c.attnum'),
        'indexes': ('SELECT i.name,i.type,i.algo,i.algo_table_type,i.algo_params,'
                    'i.column_name,i.ordinal_position,i.index_table_name,i.included_columns '
                    'FROM mo_catalog.mo_indexes i JOIN mo_catalog.mo_tables t ON i.table_id=t.rel_id '
                    'WHERE ' + scope + ' ORDER BY i.name,i.ordinal_position,i.index_table_name,i.algo_table_type,i.column_name'),
        'checks': ('SELECT cc.constraint_name,cc.check_clause,cc.enforced '
                   'FROM mo_check_constraints() cc WHERE ' + _same_name('cc.constraint_schema', _literal(database)) +
                   ' AND ' + _same_name('cc.table_name', _literal(table)) + ' ORDER BY cc.constraint_name'),
        'foreign_keys': ('SELECT f.constraint_name,f.column_name,f.refer_db_name,'
                         'f.refer_table_name,f.refer_column_name FROM mo_catalog.mo_foreign_keys f '
                         # Current MO legitimately persists zero FK IDs; its
                         # tenant-local FK dictionary is joined by names too.
                         'JOIN mo_catalog.mo_tables t ON ' + _same_name('f.db_name', 't.reldatabase') +
                         ' AND ' + _same_name('f.table_name', 't.relname') + ' '
                         'WHERE ' + scope + ' ORDER BY f.constraint_name,f.column_name'),
    }
    widths = {'table': 3, 'columns': 6, 'indexes': 9, 'checks': 3, 'foreign_keys': 5}
    metadata = {label: rows(label, sql, widths[label]) for label, sql in metadata_sql.items()}
    if len(metadata['table']) != 1 or metadata['table'][0][1] != 'r':
        raise IntegrityError('audit requires exactly one ordinary base table')
    partitioned = metadata['table'][0][2]
    if partitioned not in (0, 1):
        raise IntegrityError('unknown partition metadata')
    if partitioned:
        report['exclusions'].append('TABLE: partitioned physical layout outside verified scope')
    if not metadata['columns']:
        raise IntegrityError('missing column metadata')
    columns = {}
    for name, notnull, hidden, dtype, collation, extra in metadata['columns']:
        _ident(name)
        if name in columns or notnull not in (0, 1) or hidden not in (0, 1):
            raise IntegrityError('ambiguous column metadata')
        columns[name] = dict(notnull=notnull, hidden=hidden, dtype=dtype,
                             collation=collation, extra=extra or '')
        if not hidden and not dtype:
            raise IntegrityError('missing visible column type metadata')
    hints = _ignore_index_hints(row[:3] for row in metadata['indexes'])
    groups = defaultdict(list)
    for row in metadata['indexes']:
        groups[row[0]].append(row)
    specialized = set()
    partition_indexes = set()
    for name, parts in groups.items():
        # Partition roots are also relkind='r', but their secondary physical
        # layout is not the ordinary table encoding audited below. Do not
        # assume one physical relation or infer a logical UNIQUE audit from
        # an unsupported partition-index definition.
        if partitioned and any(row[1] != 'PRIMARY' for row in parts):
            partition_indexes.add(name)
            continue
        # Vector/fulltext families legitimately have several physical tables
        # and repeated ordinals for one logical name. Do not apply B-tree
        # one-table metadata invariants before excluding those families.
        if any(row[1] not in ('PRIMARY', 'UNIQUE', 'MULTIPLE') or
               str(row[2] or '').lower() not in ('', 'btree') or row[3] for row in parts):
            specialized.add(name)
            continue
        if [row[6] for row in parts] != list(range(1, len(parts) + 1)):
            raise IntegrityError('incomplete index key metadata: ' + _tag(name))
        if len({(row[1], row[2], row[3], row[4], row[7], row[8]) for row in parts}) != 1:
            raise IntegrityError('ambiguous index definition: ' + _tag(name))
    primary = [parts for name, parts in groups.items()
               if name not in specialized and name not in partition_indexes and parts[0][1] == 'PRIMARY']
    if len(primary) > 1:
        raise IntegrityError('ambiguous primary key')
    pk_names = [row[5] for row in primary[0]] if primary else []
    if not pk_names:
        if '__mo_fake_pk_col' not in columns:
            raise IntegrityError('missing primary/fake-primary key metadata')
        pk_names = ['__mo_fake_pk_col']
    if any(name not in columns for name in pk_names):
        raise IntegrityError('unknown primary key column')
    pk_expr = (_ident(pk_names[0]) if len(pk_names) == 1 else
               'serial(' + ','.join(_ident(name) for name in pk_names) + ')')
    base = target + hints

    def excluded(name, reason):
        report['exclusions'].append(name + ': ' + reason)

    def unsupported(names):
        for name in names:
            if name in ('__mo_cpkey_col', '__mo_fake_pk_col'):
                continue
            col = columns.get(name)
            if col is None:
                raise IntegrityError('unknown index column: ' + _tag(name))
            if col['hidden'] or 'GENERATED' in col['extra'].upper():
                return 'generated/expression key not supported'
            if col['collation'] in ('utf8_unicode_ci', 'utf8mb4_unicode_ci'):
                return 'native Unicode opaque key format not exposed by catalog'
            if str(col['dtype']).lower() not in {
                'tinyint', 'smallint', 'mediumint', 'int', 'integer', 'bigint',
                'decimal', 'numeric', 'char', 'varchar', 'binary', 'varbinary',
                'date', 'datetime', 'time', 'timestamp', 'year', 'bool', 'boolean', 'bit', 'enum', 'set',
            }:
                return 'key data type outside verified encoding scope'
        return None

    def violation(label, predicate, source=base):
        count, = metrics(label, 'SELECT COUNT(*) FROM ' + source + ' WHERE ' + predicate, 1)
        if count:
            raise IntegrityError('integrity violation: ' + _tag(label) + ' count=' + str(count))
        report['constraints'] += 1

    # A count of each group, not COUNT(DISTINCT a,b): the latter's NULL
    # semantics and optimizer rewrites can obscure the checked key domain.
    def unique(label, names):
        predicate = ' AND '.join(_ident(name) + ' IS NOT NULL' for name in names)
        # ENUM ordinal 0 and a declared empty-string member can both render
        # as ''. They are distinct keys: use the public numeric ordinal, not
        # the projected label, for logical uniqueness.
        key_parts = ['CAST(' + _ident(name) + ' AS UNSIGNED)' if
                     str(columns[name]['dtype']).lower() == 'enum' else _ident(name)
                     for name in names]
        key = 'HEX(serial_full(' + ','.join(key_parts) + '))'
        count, distinct, duplicates = metrics('unique:' + label,
            'SELECT COALESCE(SUM(n),0),COUNT(*),COALESCE(SUM(n-1),0) FROM '
            '(SELECT COUNT(*) n FROM ' + base + ' WHERE ' + predicate +
            ' GROUP BY ' + key + ') integrity_unique', 3)
        if count != distinct or duplicates:
            raise IntegrityError('integrity violation: unique:' + _tag(label))
        report['constraints'] += 1

    for name, col in columns.items():
        # __mo_rowid is engine bookkeeping (ROWID is not SQL-serializable),
        # not a declared table constraint. Fake/composite PK NULL state is
        # checked separately below through its logical key expression.
        if col['notnull'] and not col['hidden']:
            # Avoid a bare ``NOT NULL column IS NULL`` predicate: planners may
            # fold that to false using precisely the metadata we are auditing.
            violation('notnull:' + name, 'HEX(serial_full(' + _ident(name) +
                      '))=HEX(serial_full(NULL))')
    pk_reason = unsupported(pk_names)
    pk_has_enum = any(str(columns[name]['dtype']).lower() == 'enum' for name in pk_names)
    if pk_reason:
        excluded('PRIMARY', pk_reason)
    else:
        unique('PRIMARY', pk_names)
        violation('primary_null', ' OR '.join('HEX(serial_full(' + _ident(name) +
                  '))=HEX(serial_full(NULL))' for name in pk_names))

    for name, parts in groups.items():
        kind, algo, family, params, physical, included = (parts[0][i] for i in (1, 2, 3, 4, 7, 8))
        if name in partition_indexes:
            reason = ('partitioned index logical UNIQUE and physical mapping not checked'
                      if any(row[1] == 'UNIQUE' for row in parts) else
                      'partitioned physical index mapping not checked')
            excluded(name, reason)
            continue
        if name in specialized:
            excluded(name or '<unnamed specialized index>', 'specialized/non-B-tree index family')
            continue
        if kind == 'PRIMARY':
            continue
        if kind not in ('UNIQUE', 'MULTIPLE') or str(algo or '').lower() not in ('', 'btree') or family:
            excluded(name, 'specialized/non-B-tree index family')
            continue
        if params and str(params).strip() not in ('', '{}'):
            excluded(name, 'prefix/index parameters outside verified scope')
            continue
        if included:
            excluded(name, 'covering index payload outside verified scope')
            continue
        if not physical:
            raise IntegrityError('missing physical index metadata: ' + _tag(name))
        # A legal user identifier can itself start with __mo_alias_. Only
        # resolve a catalog alias if no exact base column has that name.
        names = [row[5] if row[5] in columns else row[5].removeprefix('__mo_alias_') for row in parts]
        reason = pk_reason or unsupported(names)
        enum_parts = any(str(columns.get(key, {}).get('dtype')).lower() == 'enum' for key in names)
        # SQL serial()/serial_full() receives an ENUM's visible string label,
        # whereas DML's stored composite key retains its internal ENUM tag.
        # A raw single UNIQUE ENUM key with a non-ENUM PK is verified. Hidden
        # ENUM primary-target metadata is not safe to decode generically even
        # for a scalar PK. Never substitute a CAST guess for either format.
        if not reason and (pk_has_enum or
                           (enum_parts and (kind == 'MULTIPLE' or len(names) > 1))):
            reason = ('ENUM primary-target decoding outside verified scope' if pk_has_enum else
                      'serialized ENUM physical key encoding outside verified scope')
        if reason:
            excluded(name, reason)
            continue
        exprs = [pk_expr if key == '__mo_cpkey_col' else _ident(key) for key in names]
        if kind == 'UNIQUE':
            unique(name, names)
            predicate = ' WHERE ' + ' AND '.join(expr + ' IS NOT NULL' for expr in exprs)
            key_expr = exprs[0] if len(exprs) == 1 else 'serial(' + ','.join(exprs) + ')'
        else:
            # mo_indexes lists the appended primary key via __mo_alias_.
            # Never guess/add a PK to incomplete metadata: its key order matters.
            if not any(row[5].startswith('__mo_alias_') and row[5] not in columns for row in parts) and not (
                len(pk_names) == 1 and pk_names[0] in names
            ):
                raise IntegrityError('ordinary index lacks primary-key part: ' + _tag(name))
            predicate = ''
            key_expr = exprs[0] if len(exprs) == 1 else 'serial_full(' + ','.join(exprs) + ')'
        actual_key = '`__mo_index_idx_col`'
        if kind == 'UNIQUE' and len(names) == 1 and enum_parts:
            # Both physical single UNIQUE and base column have the same ENUM
            # ordinal domain. Explicitly avoid lossy label projection on BOTH
            # sides (especially ordinal 0 versus a declared empty member).
            key_expr = 'CAST(' + key_expr + ' AS UNSIGNED)'
            actual_key = 'CAST(`__mo_index_idx_col` AS UNSIGNED)'
        expected = 'HEX(serial_full(' + key_expr + ',' + pk_expr + '))'
        actual = 'HEX(serial_full(' + actual_key + ',`__mo_index_pri_col`))'
        # UNION ALL keeps duplicates. The encoded pair includes both key and
        # target identity; equal row counts with stale/wrong mappings fail.
        sql = ('SELECT COALESCE(SUM(e),0),COALESCE(SUM(a),0),'
               'COALESCE(SUM(CASE WHEN e>a THEN e-a ELSE 0 END),0),'
               'COALESCE(SUM(CASE WHEN a>e THEN a-e ELSE 0 END),0) FROM '
               '(SELECT signature,SUM(e) e,SUM(a) a FROM ('
               'SELECT ' + expected + ' signature,1 e,0 a FROM ' + base + predicate +
               ' UNION ALL SELECT ' + actual + ' signature,0 e,1 a FROM ' +
               _ident(database) + '.' + _ident(physical) +
               ') integrity_entries GROUP BY signature) integrity_diff')
        expected_count, actual_count, missing, extra = metrics('physical:' + name, sql, 4)
        if expected_count != actual_count or missing or extra:
            raise IntegrityError('integrity violation: physical:' + _tag(name) +
                                 ' expected=' + str(expected_count) + ' actual=' + str(actual_count) +
                                 ' missing=' + str(missing) + ' extra=' + str(extra))
        report['indexes'] += 1

    for name, clause, enforced in metadata['checks']:
        if str(enforced).upper() == 'NO':
            excluded('CHECK ' + name, 'not enforced')
            continue
        if str(enforced).upper() != 'YES' or not isinstance(clause, str) or not clause:
            raise IntegrityError('invalid CHECK metadata')
        if any(token in clause for token in (';', '--', '/*', '*/')):
            excluded('CHECK ' + name, 'clause outside safe expression scope')
            continue
        # SQL CHECK accepts TRUE or UNKNOWN; only FALSE violates it.
        violation('check:' + name, '(' + clause + ') IS FALSE')

    fks = defaultdict(list)
    for row in metadata['foreign_keys']:
        fks[row[0]].append(row)
    for name, parts in fks.items():
        destinations = {(row[2], row[3]) for row in parts}
        if len(destinations) != 1 or len({row[1] for row in parts}) != len(parts):
            raise IntegrityError('ambiguous FK metadata')
        db, parent = next(iter(destinations))
        for _, child_col, _, _, parent_col in parts:
            if child_col not in columns:
                raise IntegrityError('unknown FK column')
            _ident(parent_col)
        parent_target = _ident(db) + '.' + _ident(parent)
        parent_indexes = rows('fk_indexes:' + name,
            'SELECT DISTINCT i.name,i.type,i.algo FROM mo_catalog.mo_indexes i JOIN mo_catalog.mo_tables t '
            'ON i.table_id=t.rel_id WHERE t.account_id=current_account_id() AND ' +
            _same_name('t.reldatabase', _literal(db)) + ' AND ' + _same_name('t.relname', _literal(parent)), 3)
        parent_hint = _ignore_index_hints(parent_indexes)
        non_null = ' AND '.join('c.' + _ident(row[1]) + ' IS NOT NULL' for row in parts)
        equality = ' AND '.join('p.' + _ident(row[4]) + '=c.' + _ident(row[1]) for row in parts)
        violation('fk:' + name, non_null + ' AND NOT EXISTS (SELECT 1 FROM ' + parent_target +
                  ' p' + parent_hint + ' WHERE ' + equality + ')', target + ' c' + hints)

    for label, sql in metadata_sql.items():
        if rows(label, sql, widths[label]) != metadata[label]:
            raise IntegrityError('metadata changed during integrity audit: ' + label)
    return report
