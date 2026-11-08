"""ghera — two fences on PostgreSQL row-level security, for a service that reaches its database as one role.

A service that connects as one role (often the owner of every table) and serves many tenants and many
applications usually fences them in its own code: every query remembers its tenant, every door
remembers whose tables it may touch. ghera moves both rules into the database, so a query that
forgets is refused by Postgres instead of answering rows it should not.

THE TENANT FENCE. Every table carrying the tenant column answers only the rows of the tenant the
current transaction declared. A sentinel value spans every tenant, for the service's own machinery
(replication, migrations, rebuilds), which says so out loud rather than by forgetting. With a deleted
column, deleted rows are hidden too unless spanning. The tables are measured, not listed: a table
that gains the column is covered at the next apply.

THE APPLICATION FENCE. Tables with no tenant column belong to one application each. A transaction
sees a table's rows only when it declared that application, or the sentinel, or the table is
declared open to the application it declared.

FAIL CLOSED. Both settings are read with current_setting(name, true), which answers NULL when unset,
so a path that declared nothing sees zero rows and may insert none.

TWO MECHANICS THAT DECIDE WHETHER ANY OF THIS ENFORCES ANYTHING:

1. ENABLE and FORCE. Postgres bypasses row-level security for a table's owner, so ENABLE alone,
   when the service connects as the owner, is policies present and nothing enforced. FORCE makes the
   owner subject to its own policies. The status read checks FORCE as its own column, because
   ENABLE-without-FORCE is the state that looks right and enforces nothing.
2. PER TRANSACTION, NEVER PER SESSION. With a pooled connection a session-level SET would carry one
   request's tenant into the next borrower's. Both settings are set with set_config(..., true), so
   they die with the transaction. The cost: anything that commits in a loop loses its scope at the
   first commit and must declare again (span_this_transaction). A SELECT that lost its scope answers
   zero rows and looks like an answer; an INSERT is refused loudly.

WHAT IT CANNOT HOLD. One process on one role: the database enforces the identity the code declares.
It cannot stop code that declares a false application, any more than per-application roles stop code
that switches roles. It removes the failure that actually happens: the honest path that never knew a
boundary was there.

A service describes itself once, in a Declaration, and calls the Ghera built from it. It talks to
Postgres through psycopg2, imported only where a connection is used.
"""
from __future__ import annotations

import contextvars
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping, Optional, Union

__all__ = ['Declaration', 'Ghera', '__version__']
__version__ = '0.1.0'

# Names and values ghera writes into DDL and policy text. The Declaration is trusted input, written
# by the service's own developers, but a name that does not match is refused rather than quoted
# around, so a value read from elsewhere can never become SQL.
_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,62}$')
_SETTING = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$')
_VALUE = re.compile(r'^[A-Za-z0-9_.:-]{1,63}$')


def _ok(kind: str, value: str, pattern) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(f'ghera: {kind} {value!r} is not a plain name')
    return value


def _nothing() -> dict:
    return {}


@dataclass(frozen=True)
class Declaration:
    """Everything ghera needs to know about one service's database. Callables are called each time
    they are needed, so a list read from elsewhere stays current."""
    tenant_setting: str                     # the transaction setting naming the tenant, e.g. 'app.tenant'
    app_setting: str                        # the transaction setting naming the application, e.g. 'app.application'
    tenant_column: str = 'tenant_id'
    deleted_column: Optional[str] = 'is_deleted'   # None: no soft delete
    span_all: str = 'ALL_TENANTS'           # the sentinel for the service's own machinery
    schema: str = 'public'
    tenant_policy: str = 'ghera_tenant'
    app_policy: str = 'ghera_application'
    owners: Callable[[], Mapping[str, str]] = _nothing        # table -> the application that owns it
    open_to: Callable[[], Mapping[str, tuple]] = _nothing     # table -> further applications that may read it
    known: Callable[[], set] = set          # tenant-free tables deliberately outside the fence
    scope_var: str = 'ghera_tenant_scope'   # the context variables' names
    app_var: str = 'ghera_app_scope'
    unclassified_noun: str = 'table'
    unclassified_hint: str = 'give it an owner, open it, or name it known'


class Ghera:
    def __init__(self, decl: Declaration):
        for kind, v, pat in (('tenant_setting', decl.tenant_setting, _SETTING),
                             ('app_setting', decl.app_setting, _SETTING),
                             ('tenant_column', decl.tenant_column, _NAME),
                             ('span_all', decl.span_all, _VALUE),
                             ('schema', decl.schema, _NAME),
                             ('tenant_policy', decl.tenant_policy, _NAME),
                             ('app_policy', decl.app_policy, _NAME)):
            _ok(kind, v, pat)
        if decl.deleted_column is not None:
            _ok('deleted_column', decl.deleted_column, _NAME)
        self.d = decl
        self._scope: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(decl.scope_var, default=None)
        self._app: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(decl.app_var, default=None)

    def _t(self, table: str) -> str:
        """A table's qualified, quoted name."""
        return f'"{self.d.schema}"."{_ok("table", table, _NAME)}"'

    # ── cursors: dict rows whatever the connection's own factory is ─────────────────────────────────
    @staticmethod
    def _cur(conn):
        # Imported here, not at the top: importing ghera (to read a declaration, to build policy
        # text) must work where psycopg2 is not installed.
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── the tenant fence ────────────────────────────────────────────────────────────────────────────
    def tenant_policy_sql(self, has_deleted: bool) -> str:
        """One table's tenant policy. The deleted half is in the SAME policy: permissive policies are
        ORed, so a second policy would read 'the tenant matches OR the row is live'."""
        g, s, col = self.d.tenant_setting, self.d.span_all, self.d.tenant_column
        tenant = (f"current_setting('{g}', true) = '{s}'"
                  f" OR {col}::text = current_setting('{g}', true)")
        live = f" AND (current_setting('{g}', true) = '{s}' OR NOT {self.d.deleted_column})" \
            if has_deleted and self.d.deleted_column else ""
        return f"({tenant}){live}"

    def tenant_tables(self, conn) -> list[tuple[str, bool]]:
        """(table, has the deleted column) for every table carrying the tenant column."""
        with self._cur(conn) as cur:
            cur.execute("""
                SELECT t.tablename,
                       bool_or(c.column_name = %s) AS has_deleted
                FROM pg_tables t
                JOIN information_schema.columns c ON c.table_name = t.tablename
                                                 AND c.table_schema = t.schemaname
                WHERE t.schemaname = %s
                GROUP BY t.tablename
                HAVING bool_or(c.column_name = %s)
                ORDER BY t.tablename
            """, [self.d.deleted_column or '', self.d.schema, self.d.tenant_column])
            return [(r['tablename'], bool(r['has_deleted'])) for r in cur.fetchall()]

    def apply_tenant(self, conn, only: Optional[list[str]] = None) -> list[str]:
        """ENABLE, FORCE and the policy on every tenant table. Idempotent; commits nothing."""
        done = []
        for table, has_deleted in self.tenant_tables(conn):
            if only is not None and table not in only:
                continue
            with conn.cursor() as cur:
                cur.execute(f'ALTER TABLE {self._t(table)} ENABLE ROW LEVEL SECURITY')
                cur.execute(f'ALTER TABLE {self._t(table)} FORCE ROW LEVEL SECURITY')
                cur.execute(f'DROP POLICY IF EXISTS {self.d.tenant_policy} ON {self._t(table)}')
                cur.execute(f'CREATE POLICY {self.d.tenant_policy} ON {self._t(table)} '
                            f'USING ({self.tenant_policy_sql(has_deleted)})')
            done.append(table)
        return done

    def remove_tenant(self, conn, only: Optional[list[str]] = None) -> list[str]:
        """The exact inverse, for drills."""
        done = []
        for table, _ in self.tenant_tables(conn):
            if only is not None and table not in only:
                continue
            with conn.cursor() as cur:
                cur.execute(f'DROP POLICY IF EXISTS {self.d.tenant_policy} ON {self._t(table)}')
                cur.execute(f'ALTER TABLE {self._t(table)} NO FORCE ROW LEVEL SECURITY')
                cur.execute(f'ALTER TABLE {self._t(table)} DISABLE ROW LEVEL SECURITY')
            done.append(table)
        return done

    def _flags(self, conn, policy: str) -> list[dict]:
        with self._cur(conn) as cur:
            cur.execute("""
                SELECT c.relname AS table,
                       c.relrowsecurity AS enabled,
                       c.relforcerowsecurity AS forced,
                       EXISTS (SELECT 1 FROM pg_policy p
                               WHERE p.polrelid = c.oid AND p.polname = %s) AS policy,
                       EXISTS (SELECT 1 FROM information_schema.columns ic
                               WHERE ic.table_schema = %s AND ic.table_name = c.relname
                                 AND ic.column_name = %s) AS has_tenant
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'm')
                ORDER BY c.relname
            """, [policy, self.d.schema, self.d.tenant_column, self.d.schema])
            return [dict(r) for r in cur.fetchall()]

    def tenant_state(self, conn) -> tuple[list[dict], list[str]]:
        """(covered, uncovered): each tenant table's enabled/forced/policy as the database reports
        them, and the tables with no tenant column."""
        rows = self._flags(conn, self.d.tenant_policy)
        keep = ('table', 'enabled', 'forced', 'policy')
        covered = [{k: r[k] for k in keep} for r in rows if r['has_tenant']]
        uncovered = [r['table'] for r in rows if not r['has_tenant']]
        return covered, uncovered

    def _warn_if_exempt(self, conn) -> None:
        """A superuser or BYPASSRLS role ignores every policy, FORCE included: say so, since a clean
        report read as such a role says nothing about what the service meets."""
        with self._cur(conn) as cur:
            cur.execute('SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user')
            r = cur.fetchone()
        if r and (r['rolsuper'] or r['rolbypassrls']):
            print(f"  WARNING: {r['rolname']} is {'a superuser' if r['rolsuper'] else 'BYPASSRLS'} — "
                  f'row-level security does not apply to it; read and drill as the service\'s own role')

    def tenant_report(self, conn) -> int:
        self._warn_if_exempt(conn)
        covered, uncovered = self.tenant_state(conn)
        bad = [r for r in covered if not (r['enabled'] and r['forced'] and r['policy'])]
        for r in bad:
            missing = [k for k in ('enabled', 'forced', 'policy') if not r[k]]
            print(f"  {r['table']}: NOT enforced — missing {', '.join(missing)}")
        print(f'{len(covered) - len(bad)}/{len(covered)} tenant tables enforced '
              f'(ENABLE + FORCE + policy); {len(uncovered)} tables carry no '
              f'{self.d.tenant_column} and take none: {", ".join(uncovered)}')
        return 1 if bad else 0

    def tenant_drill(self, conn) -> int:
        """Prove the tenant fence on this database: on the tenant table with the most rows, each of
        two tenants sees exactly its own rows, spanning sees all, and an undeclared transaction sees
        none. Every arm is a measured count against the spanning truth."""
        def count(table: str, value: Optional[str], where: str = '') -> int:
            self.set_tenant(conn, value if value is not None else '')
            with self._cur(conn) as cur:
                cur.execute(f'SELECT count(*) AS n FROM {self._t(table)} {where}')
                n = cur.fetchone()['n']
            conn.rollback()
            return n

        failures = []

        def check(label: str, cond: bool) -> None:
            print(f'  {"ok  " if cond else "FAIL"} {label}')
            if not cond:
                failures.append(label)

        best, best_n = None, 0
        for table, _ in self.tenant_tables(conn):
            n = count(table, self.d.span_all)
            if n > best_n:
                best, best_n = table, n
        if best is None:
            check('a tenant table with rows to prove refusal against', False)
            return 1
        self.set_tenant(conn, self.d.span_all)
        with self._cur(conn) as cur:
            col = self.d.tenant_column
            live = f'WHERE NOT {self.d.deleted_column}' if self.d.deleted_column and \
                dict(self.tenant_tables(conn)).get(best) else ''
            cur.execute(f'SELECT {col}::text AS t, count(*) AS n FROM {self._t(best)} {live} GROUP BY 1 ORDER BY 2 DESC LIMIT 2')
            per = [(r['t'], r['n']) for r in cur.fetchall()]
        conn.rollback()
        print(f'ground truth under the sentinel: {best}={best_n}; ' + ', '.join(f'tenant {t}: {n}' for t, n in per))
        for t, n in per:
            check(f'tenant {t} sees exactly its own live rows of {best} ({n})', count(best, t) == n)
        check('the drill table holds rows of two tenants, so isolation can be proved', len(per) == 2)
        if len(per) == 2:
            other = _ok('tenant', per[1][0], _VALUE)
            check(f'tenant {per[0][0]} sees none of tenant {other}\'s rows',
                  count(best, per[0][0], f"WHERE {self.d.tenant_column}::text = '{other}'") == 0)
        check('no declared tenant sees nothing (fail closed)', count(best, None) == 0)
        print(f'{"DRILL PASS" if not failures else "DRILL FAIL"} — {len(failures)} failure(s)')
        return 1 if failures else 0

    # ── the application fence ───────────────────────────────────────────────────────────────────────
    def app_policy_sql(self, table: str, owner: str) -> str:
        g, a, s = self.d.tenant_setting, self.d.app_setting, self.d.span_all
        arms = [f"current_setting('{g}', true) = '{s}'",
                f"current_setting('{a}', true) = '{_ok('application', owner, _VALUE)}'"]
        for app in self.d.open_to().get(table, ()):
            if _ok('application', app, _VALUE) != owner:
                arms.append(f"current_setting('{a}', true) = '{app}'")
        return '(' + ' OR '.join(arms) + ')'

    def apply_apps(self, conn) -> list[str]:
        """Fence every owned table that exists; one not created yet is named and skipped, so a deploy
        may run this before the migration that creates it and again after."""
        tenant_tables = {t for t, _ in self.tenant_tables(conn)}
        both = sorted(tenant_tables & set(self.d.owners()))
        if both:
            # Permissive policies are ORed: an application policy on a tenant table would let its
            # owner read every tenant's rows. A table is fenced by tenant or by application, never both.
            raise ValueError(f'ghera: {", ".join(both)} carry {self.d.tenant_column} and also have an owner')
        done = []
        for table, owner in sorted(self.d.owners().items()):
            with self._cur(conn) as cur:
                cur.execute('SELECT to_regclass(%s) AS t', [self._t(table)])
                if cur.fetchone()['t'] is None:
                    print(f'not yet created, fenced on the run after its migration: {table}')
                    continue
                cur.execute(f'ALTER TABLE {self._t(table)} ENABLE ROW LEVEL SECURITY')
                cur.execute(f'ALTER TABLE {self._t(table)} FORCE ROW LEVEL SECURITY')
                cur.execute(f'DROP POLICY IF EXISTS {self.d.app_policy} ON {self._t(table)}')
                cur.execute(f'CREATE POLICY {self.d.app_policy} ON {self._t(table)} '
                            f'USING {self.app_policy_sql(table, owner)}')
            done.append(table)
        return done

    def remove_apps(self, conn) -> list[str]:
        """Drop the policy and turn row-level security off on every owned table that exists."""
        done = []
        for table in sorted(self.d.owners()):
            with self._cur(conn) as cur:
                cur.execute('SELECT to_regclass(%s) AS t', [self._t(table)])
                if cur.fetchone()['t'] is None:
                    continue
                cur.execute(f'DROP POLICY IF EXISTS {self.d.app_policy} ON {self._t(table)}')
                cur.execute(f'ALTER TABLE {self._t(table)} NO FORCE ROW LEVEL SECURITY')
                cur.execute(f'ALTER TABLE {self._t(table)} DISABLE ROW LEVEL SECURITY')
            done.append(table)
        return done

    def app_state(self, conn) -> tuple[list[dict], list[str]]:
        """(fenced-table states, unclassified tables): an unclassified table has no tenant column
        and is neither owned nor known — the thing the status read exists to catch."""
        own = self.d.owners()
        rows = self._flags(conn, self.d.app_policy)
        fenced = [r for r in rows if r['table'] in own]
        known = set(own) | set(self.d.known())
        unclassified = [r['table'] for r in rows if not r['has_tenant'] and r['table'] not in known]
        return fenced, unclassified

    def app_report(self, conn) -> int:
        fenced, unclassified = self.app_state(conn)
        bad = [r for r in fenced if not (r['enabled'] and r['forced'] and r['policy'])]
        for r in bad:
            missing = [k for k in ('enabled', 'forced', 'policy') if not r[k]]
            print(f"  {r['table']}: NOT fenced — missing {', '.join(missing)}")
        for t in unclassified:
            print(f'  {t}: {self.d.unclassified_noun} UNCLASSIFIED — {self.d.unclassified_hint}')
        print(f'{len(fenced) - len(bad)}/{len(fenced)} fenced tables enforced '
              f'(ENABLE + FORCE + policy); {len(unclassified)} unclassified {self.d.unclassified_noun}(s)')
        return 1 if bad or unclassified else 0

    def app_drill(self, conn, home: tuple[str, str], other: tuple[str, str]) -> int:
        """Prove the application fence both ways: `home` and `other` are (application, a table it
        owns). Each sees its own; `other` is refused home's table; home sees other's table only if
        it is declared open to home; nothing declared sees nothing."""
        def count(table: str, app: Optional[str], span: bool = False) -> int:
            self.set_tenant(conn, self.d.span_all if span else '')
            with conn.cursor() as cur:
                cur.execute('SELECT set_config(%s, %s, true)', [self.d.app_setting, app or ''])
            with self._cur(conn) as cur:
                cur.execute(f'SELECT count(*) AS n FROM {self._t(table)}')
                n = cur.fetchone()['n']
            conn.rollback()
            return n

        (h_app, h_table), (o_app, o_table) = home, other
        failures = []

        def check(label: str, cond: bool) -> None:
            print(f'  {"ok  " if cond else "FAIL"} {label}')
            if not cond:
                failures.append(label)

        truth_h = count(h_table, None, span=True)
        truth_o = count(o_table, None, span=True)
        print(f'ground truth under the sentinel: {h_table}={truth_h}, {o_table}={truth_o}')
        check('both drill tables hold rows, so every refusal is a measured zero', truth_h > 0 and truth_o > 0)
        check(f"{o_app} sees its own {o_table} ({truth_o})", count(o_table, o_app) == truth_o)
        check(f"{o_app} is refused {h_app}'s {h_table} (0 of {truth_h})", count(h_table, o_app) == 0)
        check(f"{h_app} sees its own {h_table} ({truth_h})", count(h_table, h_app) == truth_h)
        opened = h_app in self.d.open_to().get(o_table, ())
        check(f"{h_app} on {o_table}: {'sees it (declared open)' if opened else 'refused'}",
              count(o_table, h_app) == (truth_o if opened else 0))
        check('no declared application sees nothing (fail closed)',
              count(h_table, None) == 0 and count(o_table, None) == 0)
        print(f'{"DRILL PASS" if not failures else "DRILL FAIL"} — {len(failures)} failure(s)')
        return 1 if failures else 0

    # ── declaring, per transaction ─────────────────────────────────────────────────────────────────
    def set_tenant(self, conn, value: str) -> None:
        """Set the tenant setting for THIS transaction on a connection you hold."""
        with conn.cursor() as cur:
            cur.execute('SELECT set_config(%s, %s, true)', [self.d.tenant_setting, value])

    def span_this_transaction(self, conn) -> None:
        """Declare the sentinel again on a connection that has just committed or rolled back."""
        self.set_tenant(conn, self.d.span_all)

    @contextmanager
    def tenant(self, conn, tenant_id: int) -> Iterator[None]:
        self.set_tenant(conn, str(int(tenant_id)))
        yield

    @contextmanager
    def spanning(self, conn) -> Iterator[None]:
        self.set_tenant(conn, self.d.span_all)
        yield

    # ── declaring, per request: a context variable a connection pool applies to each transaction ───
    def _norm(self, value: Union[int, str]) -> str:
        return self.d.span_all if value == self.d.span_all else str(int(value))

    @contextmanager
    def scope(self, value: Union[int, str]) -> Iterator[None]:
        token = self._scope.set(self._norm(value))
        try:
            yield
        finally:
            self._scope.reset(token)

    def set_scope(self, value: Union[int, str]) -> None:
        self._scope.set(self._norm(value))

    def current_scope(self) -> Optional[str]:
        return self._scope.get()

    def apply_scope(self, conn) -> None:
        """Call at the top of every transaction. Silent when nothing is declared: the policy's empty
        answer is the signal, not an exception."""
        v = self._scope.get()
        if v is not None:
            self.set_tenant(conn, v)

    def set_app(self, value: str) -> None:
        self._app.set(str(value))

    @contextmanager
    def app_scope(self, value: str) -> Iterator[None]:
        token = self._app.set(str(value))
        try:
            yield
        finally:
            self._app.reset(token)

    def current_app(self) -> Optional[str]:
        return self._app.get()

    def apply_app_scope(self, conn) -> None:
        v = self._app.get()
        if v is not None:
            with conn.cursor() as cur:
                cur.execute('SELECT set_config(%s, %s, true)', [self.d.app_setting, v])
