# ghera

*Ghera* (घेरा) is Hindi for an enclosure. ghera puts two fences around the tables of a PostgreSQL database
and makes the database enforce them. It is for a service that reaches its database as one role, usually
the owner of every table, while serving many tenants and many applications.

Such a service usually fences in its own code: every query remembers its tenant, and every door remembers
whose tables it may touch. ghera moves both rules into row-level security. A query that forgets is then
refused by Postgres. It does not quietly answer rows it should not have seen.

By Umesh Mittal. Copyright Ploutos Services LLC, under the Apache License 2.0.

## The two fences

**Tenant.** Every table with the tenant column answers only the rows of the tenant the current
transaction declared. With a soft-delete column, deleted rows are hidden too. The tables are found by
their column, not listed, so a table that gains the column is covered at the next `--apply`.

**Application.** A table with no tenant column belongs to one application. A transaction sees its rows
only when it declared that application, or the table is declared open to the application it declared.

A **sentinel** value of the tenant setting spans every tenant and every application. It is for the
service's own machinery, such as replication, migrations and rebuilds. That machinery says so out loud
instead of getting through by forgetting.

**Fail closed.** A transaction that declared nothing sees zero rows and may insert none.

## Two things that decide whether any of this works

1. **FORCE.** Postgres lets a table's owner bypass row-level security. A service connected as the owner is
   therefore not fenced by `ENABLE ROW LEVEL SECURITY` alone: the policies exist and enforce nothing.
   ghera always sets `FORCE` as well, and its status read checks `FORCE` on its own line.
2. **Per transaction.** Both settings are set with `set_config(name, value, true)`, so they end with the
   transaction. A pooled connection never hands one request's tenant to the next borrower. The cost is
   that anything committing in a loop loses its scope at the first `COMMIT` and must declare again
   (`span_this_transaction`). A `SELECT` that lost its scope returns zero rows and looks like a real
   answer.

## What it does not do, in this version

- **A tenant cannot soft-delete its own row.** The policy has no separate `WITH CHECK`, so an update that
  sets the deleted column to true breaks the visibility rule and is refused. Soft-delete under the
  sentinel.
- **Open to means read and write.** An application a table is open to may write it as well as read it.
- **No index on the comparison.** The tenant column is compared as text (`tenant_id::text =
  current_setting(...)`), so an index on an integer tenant column is not used for that check.
- **`remove` turns row-level security off** on each fenced table. Any policy of your own on that table
  stops applying too.
- **It reads one schema** (`public` by default), and reports tables, partitioned tables and materialized
  views. A materialized view refreshed under the sentinel holds every tenant's rows: give it a policy of its
  own, or do not serve it.

ghera cannot stop code that declares a false identity, just as separate roles cannot stop code that
switches roles. What it removes is the failure that actually happens: an honest code path that never knew
a boundary was there.

## Install

    pip install git+https://github.com/ploutosservices/ghera

ghera needs Python 3.10 or later, psycopg2 and PostgreSQL 13 or later. Its drill has been run on PostgreSQL
17 with Python 3.13; the compose demo uses PostgreSQL 16 and Python 3.12.

## Use

Describe your database once. The declaration is trusted input: it is written into DDL. ghera therefore
refuses any name or value that is not a plain identifier, so a value read from elsewhere can never become
SQL.

```python
from ghera import Declaration, Ghera

DECL = Declaration(
    tenant_setting='app.tenant',          # the per-transaction settings the policies read
    app_setting='app.application',
    tenant_column='tenant_id',
    deleted_column='is_deleted',          # or None
    owners=lambda: {'recipe': 'kitchen', 'plot': 'garden'},
    open_to=lambda: {'plot': ('kitchen',)},
    known=lambda: {'schema_version'},     # tenant-free tables you leave outside the fence on purpose
)
FENCE = Ghera(DECL)
```

Tenant ids are integers. The deleted column is a boolean. A table is fenced by tenant or by
application, never both: `--apply` refuses a table that carries the tenant column and also has an owner.

At deploy time, apply it and read it back. Both commands are idempotent, and the read exits 1 on any gap,
including a tenant-free table nobody classified:

    python -m ghera --dsn "$DSN" --declaration myapp.fence:DECL --apply
    python -m ghera --dsn "$DSN" --declaration myapp.fence:DECL

In the service, declare once per request with the context managers, and apply the declaration at the
start of every transaction you open:

```python
with FENCE.scope(tenant_id), FENCE.app_scope('kitchen'):
    with conn:                          # one transaction (psycopg2: autocommit off)
        FENCE.apply_scope(conn)
        FENCE.apply_app_scope(conn)
        ...
```

psycopg2's pool has no hook that runs after `BEGIN`, so call the two `apply_*` methods yourself where your
code opens a transaction, for example in the function that hands out connections. With `autocommit=True`
every statement is its own transaction, and a scope applied before it is already gone. `set_scope` and
`set_app` exist for code that learns the tenant partway through a request. They are never reset, so use
them only where each request runs in its own context (an asyncio task, a fresh thread). On a reused worker
thread they would leak into the next request.

Prove it on a live database, measured against real counts:

    python -m ghera --dsn "$DSN" --declaration myapp.fence:DECL --drill --home kitchen:recipe --other garden:plot

Run the drill as the role your service connects as. A superuser ignores row-level security, `FORCE`
included, so a drill run as one proves nothing.

## Try it

    docker compose run --rm drill     then     docker compose down -v

This starts PostgreSQL and loads the demo (`demo/schema.sql`, `demo/declaration.py`). It then proves
each claim above as a non-superuser owner, including the limits listed, and drops what it made.
`tests/drill.py` lists every check. Without Docker, point it at any PostgreSQL where you may create a
role and a database:

    ADMIN_DSN='host=localhost user=postgres dbname=postgres' python tests/drill.py

## Words used here

- **tenant**: the customer a row belongs to, named by the tenant column.
- **application**: a program that owns tables of its own in the same database.
- **sentinel**: the tenant value that means "the service's own machinery: every row".
- **declare**: set the tenant or application for the current transaction.
