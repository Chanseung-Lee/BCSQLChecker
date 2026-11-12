"""
compare the staged BC rows with the SS4 table, entirely in SQL.

One finding per problem:
  missing_in_ss4  a BC row with no SS4 partner
  missing_in_bc   an SS4 row (in scope) with no BC partner
  mismatch        a paired row where one field differs
"""

from dataclasses import dataclass

from sqlalchemy.engine import Connection

from spec import FieldMap, Spec
from stage import stage_table_name


@dataclass
class Finding:
    kind: str                 # missing_in_ss4 | missing_in_bc | mismatch
    severity: str             # error | warn | info
    key: str                  # the row's key value(s), joined with " | "
    column: str | None = None
    bc_value: str | None = None
    ss4_value: str | None = None


def ss4_source(spec: Spec) -> str:
    """The SS4 side before the scope filter: one table, or the spec's query
    (which may join several tables)."""
    if spec.ss4.query:
        return f"({spec.ss4.query}) AS src"
    return ".".join(f"[{part}]" for part in [spec.ss4.database, *spec.ss4.table.split(".")])


def ss4_scoped(spec: Spec) -> str:
    """The SS4 side limited to the spec's scope, as a derived table."""
    where = f" WHERE {spec.ss4.where}" if spec.ss4.where else ""
    return f"(SELECT * FROM {ss4_source(spec)}{where})"


# Surrounding characters a comparison ignores: space, tab, line feed, carriage
# return. LTRIM/RTRIM remove spaces only, so a value ending in a line break
# read as a difference -- while Python's strip() had already removed it from
# the BC side. Both sides must be trimmed the same way.
WHITESPACE = "CHAR(32) + CHAR(9) + CHAR(10) + CHAR(13)"


def trimmed(expr: str, length: int = 4000) -> str:
    """Text with surrounding whitespace and line breaks removed."""
    return f"TRIM({WHITESPACE} FROM CAST({expr} AS nvarchar({length})))"


def ss4_key(k: str, alias: str = "s") -> str:
    """An SS4 key column as trimmed text, so it can match the staged text key."""
    return f"{trimmed(f'{alias}.[{k}]', 450)} COLLATE DATABASE_DEFAULT"


def join_on(spec: Spec) -> str:
    return " AND ".join(f"b.[{k}] COLLATE DATABASE_DEFAULT = {ss4_key(k)}" for k in spec.ss4.key)


def key_expr(spec: Spec, alias: str) -> str:
    """The row's key for display: k1 | k2 | ..."""
    return " + N' | ' + ".join(
        f"ISNULL(CAST({alias}.[{k}] AS nvarchar(450)), N'')" for k in spec.ss4.key)


def norm(expr: str, f: FieldMap) -> str:
    """How a value is compared. Text: trimmed, blank = NULL, one collation."""
    if f.type in ("text", "enum"):
        return f"NULLIF({trimmed(expr)}, N'') COLLATE DATABASE_DEFAULT"
    return expr


def bc_is_blank(expr: str, f: FieldMap) -> str:
    """SQL that is true when the BC value counts as blank."""
    if f.type in ("text", "enum"):
        return f"NULLIF({trimmed(expr)}, N'') IS NULL"
    if f.type in ("decimal", "int"):
        return f"ISNULL({expr}, 0) = 0"
    return f"{expr} IS NULL"


def mismatch_sql(spec: Spec, stage: str, f: FieldMap) -> str:
    skip_blank = f" AND NOT ({bc_is_blank(f'b.[{f.ss4}]', f)})" if f.bc_blank == "ignore" else ""
    return f"""
SELECT 'mismatch', 'error', {key_expr(spec, 'b')}, N'{f.ss4}',
       CAST(b.[{f.ss4}] AS nvarchar(4000)), CAST(s.[{f.ss4}] AS nvarchar(4000))
  FROM {stage} b
  JOIN {ss4_scoped(spec)} s ON {join_on(spec)}
 WHERE EXISTS (SELECT {norm(f'b.[{f.ss4}]', f)} EXCEPT SELECT {norm(f's.[{f.ss4}]', f)}){skip_blank}"""

def duplicates_sql(spec: Spec, stage: str, side: str) -> str:
    """Keys that appear more than once on one side, after the same trimming
    the join uses -- so ' 12698923' and '12698923' count as the same key."""
    if side == "ss4":
        keys = [ss4_key(k) for k in spec.ss4.key]
        source, value_cols = f"{ss4_scoped(spec)} s", "NULL, CAST(COUNT(*) AS nvarchar(20)) + N' rows'"
    else:
        keys = [f"b.[{k}] COLLATE DATABASE_DEFAULT" for k in spec.ss4.key]
        source, value_cols = f"{stage} b", "CAST(COUNT(*) AS nvarchar(20)) + N' rows', NULL"
    display = " + N' | ' + ".join(f"ISNULL({k}, N'')" for k in keys)
    return f"""
SELECT 'duplicate_in_{side}', 'error', {display}, NULL, {value_cols}
  FROM {source}
 GROUP BY {', '.join(keys)}
HAVING COUNT(*) > 1"""

def run_diff(conn: Connection, spec: Spec) -> list[Finding]:
    """All findings for one spec. The BC rows must already be staged on conn."""
    stage = stage_table_name(spec)
    parts = []
    parts.append(duplicates_sql(spec, stage, "ss4"))
    parts.append(duplicates_sql(spec, stage, "bc"))

    if spec.missing_in_ss4 != "ignore":
        parts.append(f"""
SELECT 'missing_in_ss4', '{spec.missing_in_ss4}', {key_expr(spec, 'b')}, NULL, NULL, NULL
  FROM {stage} b
 WHERE NOT EXISTS (SELECT 1 FROM {ss4_scoped(spec)} s WHERE {join_on(spec)})""")

    if spec.missing_in_bc != "ignore":
        parts.append(f"""
SELECT 'missing_in_bc', '{spec.missing_in_bc}', {key_expr(spec, 's')}, NULL, NULL, NULL
  FROM {ss4_scoped(spec)} s
 WHERE NOT EXISTS (SELECT 1 FROM {stage} b WHERE {join_on(spec)})""")

    for f in spec.fields:
        if f.ss4 not in spec.ss4.key:
            parts.append(mismatch_sql(spec, stage, f))

    sql = "\nUNION ALL\n".join(parts)
    rows = conn.exec_driver_sql(sql).fetchall()
    return [Finding(*row) for row in rows]