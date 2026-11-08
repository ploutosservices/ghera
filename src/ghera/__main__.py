"""ghera's command line: apply, remove, read and drill both fences on one database.

    python -m ghera --dsn DSN --declaration MODULE:NAME [--apply | --remove | --drill]
                    [--home APP:TABLE --other APP:TABLE]

MODULE:NAME names a ghera.Declaration (import path, then attribute). With no action the command
reads both fences and exits 1 when a tenant table lacks ENABLE, FORCE or its policy, or a tenant-free
table is unclassified. --drill proves the tenant fence, and the application fence too when --home and
--other each name an application and a table it owns.

The DSN should be the role the service itself connects as. A superuser ignores row-level security,
FORCE included, so a drill run as one proves nothing.
"""
from __future__ import annotations

import argparse
import importlib
import sys

from . import Ghera


def _declaration(spec: str):
    module, _, name = spec.partition(':')
    if not name:
        raise SystemExit('--declaration takes MODULE:NAME')
    return getattr(importlib.import_module(module), name)


def _pair(spec: str) -> tuple[str, str]:
    app, _, table = spec.partition(':')
    if not table:
        raise SystemExit(f'{spec!r}: expected APP:TABLE')
    return app, table


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='python -m ghera', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dsn', required=True)
    ap.add_argument('--declaration', required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true', help='ENABLE, FORCE and the policies (idempotent)')
    g.add_argument('--remove', action='store_true', help='the exact inverse, for drills')
    g.add_argument('--drill', action='store_true', help='prove the refusal against live counts')
    ap.add_argument('--home')
    ap.add_argument('--other')
    args = ap.parse_args(argv)

    import psycopg2
    engine = Ghera(_declaration(args.declaration))
    conn = psycopg2.connect(args.dsn)
    try:
        engine.span_this_transaction(conn)
        if args.apply:
            t, a = engine.apply_tenant(conn), engine.apply_apps(conn)
            conn.commit()
            print(f'applied {engine.d.tenant_policy} to {len(t)} tables, {engine.d.app_policy} to {len(a)}')
        elif args.remove:
            t, a = engine.remove_tenant(conn), engine.remove_apps(conn)
            conn.commit()
            print(f'removed {engine.d.tenant_policy} from {len(t)} tables, {engine.d.app_policy} from {len(a)}')
            return 0
        elif args.drill:
            rc = engine.tenant_drill(conn)
            if args.home and args.other:
                rc |= engine.app_drill(conn, _pair(args.home), _pair(args.other))
            return rc
        engine.span_this_transaction(conn)
        rc = engine.tenant_report(conn)
        return rc | engine.app_report(conn)
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
