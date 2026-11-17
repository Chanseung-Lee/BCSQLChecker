"""
Shows one record from both sides, field by field, using a spec -- for checking
a finding by hand.

    python lookup.py specs/proposed/salesOrders.yaml S-ORD101190
    python lookup.py specs/partsUS.yaml 12698923

For a spec with a multi-part key, give one value per part, in order.

SS4 is searched WITHOUT the spec's where filter, and each row says whether it
is in scope. So a record that exists but is filtered out (flagged deleted,
another company) shows up as such, instead of looking missing.
"""

import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from connections import BcClient, BcTarget, ss4_engine
from diff import ss4_source, trimmed
from spec import FieldMap, load_spec
from stage import bc_url, convert


def comparable(value, f: FieldMap):
    """A value from either side, normalized the way the diff compares it."""
    if value is None:
        return None
    if f.type in ("text", "enum"):
        return str(value).strip() or None
    if f.type in ("decimal", "int"):
        try:
            return Decimal(str(value)).normalize()
        except InvalidOperation:
            return str(value)
    if f.type == "bool":
        return bool(value)
    if f.type == "guid":
        return str(value).lower()
    if f.type == "date":
        return str(value)[:10]
    if f.type == "datetime":
        if isinstance(value, datetime):
            if value.tzinfo:
                value = value.astimezone(timezone.utc)
            return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return str(value)[:23]
    return str(value)


def show(value) -> str:
    return "(blank)" if value is None else repr(value)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: python lookup.py <spec file> <key value> [more key parts]")
    spec = load_spec(sys.argv[1])
    key_values = sys.argv[2:]
    if len(key_values) != len(spec.bc.key):
        raise SystemExit(f"this spec's key has {len(spec.bc.key)} part(s): {spec.bc.key}")

    # ---- BC
    target = BcTarget.from_env(spec.bc.target)
    bc_filter = " and ".join(f"{field} eq '{value.replace(chr(39), chr(39) * 2)}'"
                             for field, value in zip(spec.bc.key, key_values))
    # Stay inside the spec's BC scope, e.g. only task 9500 planning lines, so a
    # same-named record outside the check is not picked up instead.
    if spec.bc.filter:
        bc_filter = f"({spec.bc.filter}) and {bc_filter}"
    bc_rows = BcClient().fetch_all(bc_url(spec, target), filter=bc_filter)
    print(f"BC  ({target.environment} / {target.company}, {spec.bc.entity}): "
          f"{len(bc_rows)} record(s) where {bc_filter}")

    # ---- SS4, unscoped, with an in-scope flag per row
    source = ss4_source(spec)
    in_scope = f"CASE WHEN {spec.ss4.where} THEN 1 ELSE 0 END" if spec.ss4.where else "1"
    match = " AND ".join(f"{trimmed(f'[{k}]', 450)} = ?" for k in spec.ss4.key)
    sql = f"SELECT *, {in_scope} AS [__in_scope] FROM {source} WHERE {match}"
    with ss4_engine().connect() as conn:
        result = conn.exec_driver_sql(sql, tuple(v.strip() for v in key_values))
        ss4_rows = [dict(r) for r in result.mappings()]
    label = f"{spec.ss4.database}.{spec.ss4.table}" if spec.ss4.table else "the spec's query"
    print(f"SS4 ({label}): {len(ss4_rows)} row(s)"
          + (f", where filter: {spec.ss4.where}" if spec.ss4.where else ""))
    for i, row in enumerate(ss4_rows, start=1):
        print(f"     row {i}: {'IN scope' if row['__in_scope'] else 'OUT of scope (filtered out by where)'}")

    if not bc_rows or not ss4_rows:
        print("\nNothing to compare field by field: the record is missing on one side.")
        return
    if len(bc_rows) > 1 or len(ss4_rows) > 1:
        print("\nMore than one record on a side -- comparing the first of each.")

    # ---- field by field
    bc, ss4 = bc_rows[0], ss4_rows[0]
    print(f"\n{'field':<44} {'result':<12} values")
    for f in spec.fields:
        bc_value = comparable(convert(bc.get(f.bc), f.type), f)
        ss4_value = comparable(ss4.get(f.ss4), f)
        if f.bc_blank == "ignore" and bc_value in (None, Decimal(0)):
            result = "not compared"
        elif bc_value == ss4_value:
            result = "same"
        else:
            result = "DIFFERENT"
        print(f"{f.bc + ' -> ' + f.ss4:<44} {result:<12} BC={show(bc_value)}  SS4={show(ss4_value)}")


if __name__ == "__main__":
    main()
