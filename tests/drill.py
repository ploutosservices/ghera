#!/usr/bin/env python3
"""ghera's own proof, on a database it builds and throws away.

    ADMIN_DSN='host=db user=postgres dbname=postgres' python tests/drill.py

ADMIN_DSN must be able to create a role and a database. The drill creates the role `ghera_owner`
(NOSUPERUSER NOBYPASSRLS: a superuser ignores row-level security, FORCE included, so a fence proved
as one proves nothing) and the database `ghera_drill` it owns, loads demo/schema.sql as that owner,
and then, connected as the owner the way a service is:

  1. before apply, the owner reads every row: the fence is not there yet;
  2. apply; the status read is clean;
  3. ENABLE without FORCE enforces nothing for the owner — the trap FORCE closes;
  4. each tenant sees its own live rows only, the sentinel sees all, nothing declared sees none;
     a tenant inserts its own row, and an INSERT naming another tenant, or no tenant, is refused
     by the policy (SQLSTATE 42501); a tenant's soft delete of its own row is refused too — the
     documented limit: the sentinel soft-deletes;
  5. each application sees its own table, is refused the other's unless it is declared open,
     and nothing declared sees none;
  6. a scope dies with its transaction: after COMMIT the same connection sees zero rows; the
     request-scoped path (scope, app_scope, apply_scope, apply_app_scope) declares as documented;
  7. a tenant-free table nobody classified turns the status read red;
  8. the command line's --drill passes; remove puts the database back.

Exit 0 when every check held. The database and role are dropped at the end whatever happens.
"""
import os
import secrets
import subprocess
import sys

import psycopg2
import psycopg2.extras

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# The package is src/ghera in the published tree; beside the drill's parent where it is developed.
PKG_PARENT = os.path.join(ROOT, 'src') if os.path.isdir(os.path.join(ROOT, 'src', 'ghera')) else os.path.dirname(ROOT)
sys.path.insert(0, PKG_PARENT)
sys.path.insert(0, ROOT)                        # demo.declaration

from ghera import Ghera                          # noqa: E402
from demo.declaration import DEMO               # noqa: E402

ADMIN = os.environ['ADMIN_DSN']
OWNER_PW = secrets.token_hex(16)                # a throwaway role's, made for this run
fails = []


def check(label, cond, got=''):
    print(f'  {"ok  " if cond else "FAIL"} {label}' + (f'  ({got})' if not cond and got != '' else ''))
    if not cond:
        fails.append(label)


def admin():
    c = psycopg2.connect(ADMIN)
    c.autocommit = True
    return c


def owner_dsn():
    return psycopg2.extensions.make_dsn(ADMIN, user='ghera_owner', password=OWNER_PW, dbname='ghera_drill')


def count(conn, table, tenant=None, app=None, where=''):
    with conn.cursor() as cur:
        cur.execute('SELECT set_config(%s, %s, true)', [DEMO.tenant_setting, tenant or ''])
        cur.execute('SELECT set_config(%s, %s, true)', [DEMO.app_setting, app or ''])
        cur.execute(f'SELECT count(*) FROM "{table}" {where}')
        n = cur.fetchone()[0]
    conn.rollback()
    return n


def refused(conn, sql, tenant=None):
    """True only when the policy refused it (SQLSTATE 42501), not on any other error."""
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT set_config(%s, %s, true)', [DEMO.tenant_setting, tenant or ''])
            cur.execute(sql)
        conn.rollback()
        return False
    except psycopg2.Error as e:
        conn.rollback()
        return e.pgcode == '42501'


def done(conn, sql, tenant=None):
    """True when the statement went through for that tenant (rolled back afterwards)."""
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT set_config(%s, %s, true)', [DEMO.tenant_setting, tenant or ''])
            cur.execute(sql)
            n = cur.rowcount
        conn.rollback()
        return n == 1
    except psycopg2.Error:
        conn.rollback()
        return False


def main():
    a = admin()
    with a.cursor() as cur:
        cur.execute('DROP DATABASE IF EXISTS ghera_drill')
        cur.execute('DROP ROLE IF EXISTS ghera_owner')
        cur.execute(f"CREATE ROLE ghera_owner LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{OWNER_PW}'")
        cur.execute('CREATE DATABASE ghera_drill OWNER ghera_owner')
    try:
        o = psycopg2.connect(owner_dsn())
        with o.cursor() as cur:
            cur.execute(open(os.path.join(ROOT, 'demo', 'schema.sql')).read())
        o.commit()
        g = Ghera(DEMO)
        S = DEMO.span_all

        print('1. before apply')
        check('the owner reads every invoice with nothing declared', count(o, 'invoice') == 6)

        print('2. apply')
        g.span_this_transaction(o)
        t, ap = g.apply_tenant(o), g.apply_apps(o)
        o.commit()
        check(f'tenant policy on invoice and note ({", ".join(t)})', t == ['invoice', 'note'])
        check(f'application policy on plot and recipe ({", ".join(ap)})', ap == ['plot', 'recipe'])
        g.span_this_transaction(o)
        check('the status read is clean', g.tenant_report(o) == 0 and g.app_report(o) == 0)
        o.rollback()

        print('3. FORCE is the half that binds the owner')
        with o.cursor() as cur:
            cur.execute('ALTER TABLE invoice NO FORCE ROW LEVEL SECURITY')
        o.commit()
        check('ENABLE without FORCE: the owner reads all 6 with nothing declared', count(o, 'invoice') == 6)
        g.span_this_transaction(o)
        check('and the status read goes red on it', g.tenant_report(o) == 1)
        o.rollback()
        g.span_this_transaction(o)
        g.apply_tenant(o, only=['invoice'])
        o.commit()
        check('FORCE again: nothing declared reads 0', count(o, 'invoice') == 0)

        print('4. the tenant fence')
        check('the sentinel sees all 6 invoices, deleted included', count(o, 'invoice', S) == 6)
        check('tenant 1 sees its 2 live invoices', count(o, 'invoice', '1') == 2)
        check('tenant 2 sees its 3', count(o, 'invoice', '2') == 3)
        check("tenant 1 sees none of tenant 2's", count(o, 'invoice', '1', where='WHERE tenant_id = 2') == 0)
        check('a table without soft delete: tenant 2 sees its 2 notes', count(o, 'note', '2') == 2)
        check('nothing declared sees no note', count(o, 'note') == 0)
        check('tenant 1 inserts its own row', done(o, 'INSERT INTO invoice (tenant_id, amount) VALUES (1, 1)', '1'))
        check("tenant 1 may not insert tenant 2's row",
              refused(o, 'INSERT INTO invoice (tenant_id, amount) VALUES (2, 1)', '1'))
        check('nothing declared may not insert at all', refused(o, 'INSERT INTO note (tenant_id, body) VALUES (1, \'x\')'))
        check('tenant 1 edits its own live row', done(o, 'UPDATE invoice SET amount = 9 WHERE id = 1', '1'))
        check('tenant 1 may not soft-delete its own row: the documented limit, the sentinel does',
              refused(o, 'UPDATE invoice SET is_deleted = true WHERE id = 1', '1')
              and done(o, 'UPDATE invoice SET is_deleted = true WHERE id = 1', S))

        print('5. the application fence')
        check('kitchen sees its 2 recipes', count(o, 'recipe', app='kitchen') == 2)
        check('garden sees its 3 plots', count(o, 'plot', app='garden') == 3)
        check('garden is refused the recipes', count(o, 'recipe', app='garden') == 0)
        check('kitchen reads the plots: declared open to it', count(o, 'plot', app='kitchen') == 3)
        check('nothing declared sees neither', count(o, 'recipe') == 0 and count(o, 'plot') == 0)
        check('the sentinel sees both', count(o, 'recipe', S) == 2 and count(o, 'plot', S) == 3)
        check('the plumbing table is outside the fence', count(o, 'schema_version') == 1)

        print('6. a scope dies with its transaction')
        with o.cursor() as cur:
            cur.execute('SELECT set_config(%s, %s, true)', [DEMO.tenant_setting, '1'])
            cur.execute('SELECT count(*) FROM invoice')
            before = cur.fetchone()[0]
        o.commit()
        with o.cursor() as cur:
            cur.execute('SELECT count(*) FROM invoice')
            after = cur.fetchone()[0]
        o.rollback()
        check(f'declared: {before}; after COMMIT, on the same connection: {after}', before == 2 and after == 0)
        with g.scope(2), g.app_scope('garden'):
            g.apply_scope(o)
            g.apply_app_scope(o)
            with o.cursor() as cur:
                cur.execute('SELECT (SELECT count(*) FROM invoice), (SELECT count(*) FROM plot), (SELECT count(*) FROM recipe)')
                inv, plots, recipes = cur.fetchone()
            o.rollback()
        check(f'scope(2) + app_scope(garden), applied per transaction: invoices {inv}, plots {plots}, recipes {recipes}',
              (inv, plots, recipes) == (3, 3, 0))
        check('outside the with-blocks nothing is declared', g.current_scope() is None and g.current_app() is None)

        print('7. an unclassified table')
        with o.cursor() as cur:
            cur.execute('CREATE TABLE stray (id int)')
        o.commit()
        g.span_this_transaction(o)
        check('a tenant-free table nobody classified turns the status read red', g.app_report(o) == 1)
        o.rollback()
        with o.cursor() as cur:
            cur.execute('DROP TABLE stray')
        o.commit()

        print('8. the command line')
        env = {**os.environ, 'PYTHONPATH': os.pathsep.join([PKG_PARENT, ROOT])}
        r = subprocess.run([sys.executable, '-m', 'ghera', '--dsn', owner_dsn(), '--declaration',
                            'demo.declaration:DEMO', '--drill', '--home', 'kitchen:recipe',
                            '--other', 'garden:plot'], env=env, capture_output=True, text=True)
        print(r.stdout.rstrip())
        check('python -m ghera --drill passes', r.returncode == 0, r.stderr.strip()[-300:])
        r = subprocess.run([sys.executable, '-m', 'ghera', '--dsn', owner_dsn(), '--declaration',
                            'demo.declaration:DEMO', '--remove'], env=env, capture_output=True, text=True)
        check('--remove exits 0', r.returncode == 0, r.stderr.strip()[-300:])
        check('removed: the owner reads every invoice and recipe again with nothing declared',
              count(o, 'invoice') == 6 and count(o, 'recipe') == 2)
        o.close()
    finally:
        with a.cursor() as cur:
            cur.execute('DROP DATABASE IF EXISTS ghera_drill WITH (FORCE)')
            cur.execute('DROP ROLE IF EXISTS ghera_owner')
        a.close()
    print(f'ghera drill: {len(fails)} failed' if fails else 'ghera drill: every check held')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
