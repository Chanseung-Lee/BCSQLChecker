"""
Turns a spec into a SQL query that returns BC-ready rows: one row per record,
one column per mapped BC field, named after the BC field. Run it in SSMS and
paste the results into the config package template.

    python spec_to_sql.py specs/proposed/customers_card.yaml

Prints the query and saves it to queries/<spec name>.sql.

Only the key and the mapped fields are in the query. Columns the spec lists as
ignored (BC defaults, fixed values like posting groups) are filled in the
template by hand.
"""

import sys
from pathlib import Path

from spec import load_spec


def spec_to_sql(spec) -> str:
    # One output column per key part and per mapped field, renamed to its BC name.
    columns = []
    for bc_name, ss4_name in zip(spec.bc.key, spec.ss4.key):
        columns.append(f"    s.[{ss4_name}] AS [{bc_name}]")
    for f in spec.fields:
        if f.ss4 not in spec.ss4.key:
            columns.append(f"    s.[{f.ss4}] AS [{f.bc}]")

    # The SS4 side: either a table, or the spec's own query.
    if spec.ss4.query:
        source = f"(\n{spec.ss4.query}\n) s"
    else:
        source = f"[{spec.ss4.database}].{spec.ss4.table} s"

    where = f"\nWHERE {spec.ss4.where}" if spec.ss4.where else ""
    order = ", ".join(f"s.[{k}]" for k in spec.ss4.key)

    return (f"-- {spec.name}: {spec.description}\n"
            f"SELECT\n" + ",\n".join(columns) + "\n"
            f"FROM {source}{where}\n"
            f"ORDER BY {order};\n")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python spec_to_sql.py <spec file>")
    spec = load_spec(sys.argv[1])
    sql = spec_to_sql(spec)
    print(sql)

    out = Path("queries") / f"{spec.name}.sql"
    out.parent.mkdir(exist_ok=True)
    out.write_text(sql, encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
