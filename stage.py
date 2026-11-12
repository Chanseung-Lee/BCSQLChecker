#fetch one spec's rowsx from BC and stage them into a #temp table on SS4, so the diff can compare both sides in plain SQL. 
#Temp tables belong to ONE connection and vanish when it closes, so the caller opens the connection and passes it in.
#Everything that needs the staged data must use that same connection.

import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.engine import Connection

from connections import BcClient, BcTarget
from spec import Spec

# Staging column type for each spec field type.
SQL_TYPES = {
    "text": "nvarchar(4000)",
    "enum": "nvarchar(4000)",
    "decimal": "decimal(38, 10)",
    "int": "bigint",
    "bool": "bit",
    "date": "date",
    "datetime": "datetime2(3)",
    "guid": "uniqueidentifier",
}
KEY_TYPE = "nvarchar(450)"   # keys are matched as text; 450 keeps them indexable

EMPTY_GUID = "00000000-0000-0000-0000-000000000000"

# BC escapes characters in option (enum) values as _xHHHH_: 'In_x0020_Review'.
ENUM_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")

def bc_url(spec: Spec, target: BcTarget) -> str:
    if spec.bc.api == "standard":
        return f"{target.api_base}/{spec.bc.entity}"
    if spec.bc.api == "odata":
        return f"{target.odata_base}/{spec.bc.entity}"
    raise NotImplementedError("custom API specs are not supported yet")


def fetch_bc(spec: Spec, client: BcClient) -> list[dict]:
    """Every BC row, with only the fields the spec uses."""
    target = BcTarget.from_env(spec.bc.target)
    wanted = [*spec.bc.key, *(f.bc for f in spec.fields)]
    return client.fetch_all(bc_url(spec, target), select=",".join(dict.fromkeys(wanted)))


def convert(value, type_: str):
    """One BC JSON value -> the value to stage, with BC's 'not set'
    sentinels turned into NULL."""
    if value is None:
        return None
    if type_ == "text":
        text = str(value).strip()
        return text or None
    if type_ == "enum":
        text = ENUM_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), str(value)).strip()
        return text or None
    if type_ == "decimal":
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    if type_ == "int":
        return int(value)
    if type_ == "bool":
        return bool(value)
    if type_ == "guid":
        return None if str(value).lower() == EMPTY_GUID else str(uuid.UUID(str(value)))
    if type_ == "date":
        return None if str(value).startswith("0001-01-01") else date.fromisoformat(str(value)[:10])
    if type_ == "datetime":
        if str(value).startswith("0001-01-01"):
            return None
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # Sent as text, not as a datetime: the bulk insert was found to drop the
        # fractional seconds of datetime parameters ('.047' arrived as '.000').
        # SQL Server reads this format into datetime2(3) exactly.
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    raise ValueError(f"unknown type '{type_}'")


def stage_table_name(spec: Spec) -> str:
    return f"#bc_{spec.name}"


def stage_bc(conn: Connection, spec: Spec, rows: list[dict]) -> str:
    """Creates #bc_<name> on this connection and loads the BC rows into it.
    Columns are named after the SS4 side, so the diff can join like for like."""
    table = stage_table_name(spec)
    key_pairs = list(zip(spec.bc.key, spec.ss4.key))
    other_fields = [f for f in spec.fields if f.ss4 not in spec.ss4.key]

    columns = [f"[{s}] {KEY_TYPE} NULL" for _, s in key_pairs]
    columns += [f"[{f.ss4}] {SQL_TYPES[f.type]} NULL" for f in other_fields]

    # No parameters on purpose: a parameterized statement runs as its own
    # batch (sp_executesql), and a #temp table created inside one vanishes
    # the moment that batch ends.
    conn.exec_driver_sql(f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table};")
    conn.exec_driver_sql(f"CREATE TABLE {table} ({', '.join(columns)});")

    # (staging column, BC field, type) for every column we load
    loaded = [(s, b, "text") for b, s in key_pairs]
    loaded += [(f.ss4, f.bc, f.type) for f in other_fields]

    names = ", ".join(f"[{s}]" for s, _, _ in loaded)
    marks = ", ".join("?" for _ in loaded)
    values = [tuple(convert(row.get(b), t) for _, b, t in loaded) for row in rows]

    if values:
        conn.exec_driver_sql(f"INSERT INTO {table} ({names}) VALUES ({marks});", values)
    return table

# ---------------------------------------------------------------- raw staging
# Used before any mapping exists: every BC field, as text, under BC's own names.

RAW_TABLE = "#bc_raw"


def fetch_bc_raw(client: BcClient, target: BcTarget, api: str, entity: str) -> list[dict]:
    """Every BC row with every field."""
    base = target.api_base if api == "standard" else target.odata_base
    return client.fetch_all(f"{base}/{entity}")


def raw_text(value) -> str | None:
    """A BC value as text for exploring, exactly as BC sent it -- surrounding
    whitespace and line breaks included, so the agent sees what is really there.
    BC's 'not set' sentinels become NULL. Nested values are dropped."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value)
    bare = text.strip()
    if bare == "" or bare.lower() == EMPTY_GUID or bare.startswith("0001-01-01"):
        return None
    return text


def stage_bc_raw(conn: Connection, rows: list[dict]) -> tuple[str, list[str]]:
    """Creates #bc_raw with one text column per BC field and loads the rows.
    Returns the table name and the field names."""
    fields = []
    for row in rows[:200]:                       # fields can be missing on some rows
        for name, value in row.items():
            if not name.startswith("@odata") and not isinstance(value, (dict, list)) \
                    and name not in fields:
                fields.append(name)

    conn.exec_driver_sql(f"IF OBJECT_ID('tempdb..{RAW_TABLE}') IS NOT NULL DROP TABLE {RAW_TABLE};")
    columns = ", ".join(f"[{f}] nvarchar(4000) NULL" for f in fields)
    conn.exec_driver_sql(f"CREATE TABLE {RAW_TABLE} ({columns});")

    if rows and fields:
        names = ", ".join(f"[{f}]" for f in fields)
        marks = ", ".join("?" for _ in fields)
        values = [tuple(raw_text(row.get(f)) for f in fields) for row in rows]
        conn.exec_driver_sql(f"INSERT INTO {RAW_TABLE} ({names}) VALUES ({marks});", values)
    return RAW_TABLE, fields

# ---------------------------------------------------------------- field profile
# Which BC fields are actually used, judged from the data before mapping starts,
# so the agent spends its effort on fields that carry information.
#
# "Filled" counts REAL values only. An unused number field holds 0 on every row
# and an unused flag holds false, so counting non-NULL values would call them
# 100% populated.

SPARSE_PCT = 5.0   # below this share of real values, a field is "sparse"


@dataclass
class FieldProfile:
    name: str
    filled: int                     # rows with a real value: not blank, not 0, not false
    total: int
    distinct: int                   # distinct real values
    top: list[tuple[str, int]]      # most common real values, with counts
    status: str                     # used | sparse | constant | empty

    @property
    def pct(self) -> float:
        return 100 * self.filled / self.total if self.total else 0.0


def is_default(text: str | None) -> bool:
    """Blank, zero or false: what an unused BC field holds."""
    if text is None or text.lower() == "false":
        return True
    try:
        return Decimal(text) == 0
    except InvalidOperation:
        return False


def profile_bc(rows: list[dict], fields: list[str]) -> list[FieldProfile]:
    """One profile per BC field.

      empty     no real values at all
      constant  every row holds the same value: carries no information, and
                would 'agree' with any constant SS4 column
      sparse    real values on under SPARSE_PCT of rows: may matter, but a
                mapping tested on few rows is weak evidence
      used      everything else
    """
    profiles = []
    for name in fields:
        values = Counter(raw_text(row.get(name)) for row in rows)
        real = Counter({v: n for v, n in values.items() if not is_default(v)})
        filled = sum(real.values())
        if filled == 0:
            status = "empty"
        elif len(values) == 1:
            status = "constant"
        elif 100 * filled / len(rows) < SPARSE_PCT:
            status = "sparse"
        else:
            status = "used"
        profiles.append(FieldProfile(name, filled, len(rows), len(real),
                                     real.most_common(3), status))
    return profiles
