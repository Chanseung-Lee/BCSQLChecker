# This is the loader. Its job is to read the YAML into Python objects and catch mistakes in it before anything touches BC or SS4,
# so that a typo in the Claude Code generated spec produces a clear error message and not a confusing SQL failure halfway through the run

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Allowed values. Anything else in a spec is a typo, and is reported as one.
SOURCES_OF_TRUTH = {"bc", "ss4", "both"}
BC_APIS = {"standard", "odata", "custom"}
FIELD_TYPES = {"text", "enum", "decimal", "int", "bool", "date", "datetime", "guid"}
MISSING_LEVELS = {"error", "warn", "info", "ignore"}
BLANK_RULES = {"compare", "ignore"}

# What may appear inside [brackets] in the SQL we generate from a spec.
SAFE_NAME = re.compile(r"^[\w .#\-]+$")

# Words that have no place in a read-only query or filter written into a spec.
FORBIDDEN_SQL = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|exec|execute|"
                           r"truncate|grant|revoke|deny|into)\b", re.IGNORECASE)


@dataclass(frozen=True)
class BcSide:
    target: str        # .env prefix, e.g. "BC"
    api: str           # standard | odata | custom
    entity: str        # e.g. "items"
    key: list[str]     # BC field(s) that identify a row
    filter: str | None = None  # OData $filter: which BC rows belong to this check


@dataclass(frozen=True)
class Ss4Side:
    # Either a single table (database + table), or a query joining several.
    database: str | None
    table: str | None
    query: str | None  # a SELECT; key, fields and where then name ITS output columns
    key: list[str]     # SS4 column(s), paired with bc.key by position
    where: str | None  # scope filter, raw SQL


@dataclass(frozen=True)
class FieldMap:
    bc: str
    ss4: str
    type: str = "text"
    bc_blank: str = "compare"   # "ignore": a blank BC value is not a mismatch


@dataclass(frozen=True)
class Spec:
    name: str
    description: str
    source_of_truth: str
    bc: BcSide
    ss4: Ss4Side
    missing_in_bc: str
    missing_in_ss4: str
    fields: list[FieldMap]
    # Columns deliberately not compared, each {bc: or ss4:, reason:}. A BC field
    # here is one that is unused (empty, constant) or has no SS4 counterpart.
    ignored: list[dict] = field(default_factory=list)


class SpecError(Exception):
    """A spec file is malformed. The message lists every problem found."""


def load_spec(path: str | Path) -> Spec:
    """Reads a spec file."""
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        return parse_spec(yaml.safe_load(f), path.name)


def parse_spec(raw, label: str = "spec") -> Spec:
    """Builds and checks a Spec from already-parsed YAML. Also used for the
    drafts the mapping agent sends as text."""
    if not isinstance(raw, dict):
        raise SpecError(f"{label}: not a YAML mapping")

    problems: list[str] = []

    def need(section: dict, key: str, where: str):
        """A required setting; records a problem instead of crashing if absent."""
        if section.get(key) in (None, ""):
            problems.append(f"{where}: '{key}' is required")
            return None
        return section[key]

    def as_list(value):
        """'key: number' and 'key: [number]' both become a list."""
        if value is None:
            return []
        return value if isinstance(value, list) else [value]

    def section(name: str) -> dict:
        """A sub-section such as bc: or ss4:, which must be a mapping."""
        value = raw.get(name) or {}
        if not isinstance(value, dict):
            problems.append(f"{name}: must be a mapping of settings")
            return {}
        return value

    fields = []
    for i, f in enumerate(raw.get("fields") or [], start=1):
        if not isinstance(f, dict):
            problems.append(f"fields #{i}: must be a mapping with 'bc' and 'ss4'")
            continue
        unknown = set(f) - {"bc", "ss4", "type", "bc_blank"}
        if unknown:
            problems.append(f"fields #{i}: unknown setting(s) {sorted(unknown)}")
        if not f.get("bc") or not f.get("ss4"):
            problems.append(f"fields #{i}: needs both 'bc' and 'ss4'")
            continue
        fields.append(FieldMap(bc=f["bc"], ss4=f["ss4"],
                               type=f.get("type", "text"),
                               bc_blank=f.get("bc_blank", "compare")))

    ignored = []
    for i, item in enumerate(raw.get("ignored") or [], start=1):
        if not isinstance(item, dict) or not (item.get("bc") or item.get("ss4")):
            problems.append(f"ignored #{i}: needs 'bc' or 'ss4', plus a 'reason'")
            continue
        unknown = set(item) - {"bc", "ss4", "reason"}
        if unknown:
            problems.append(f"ignored #{i}: unknown setting(s) {sorted(unknown)}")
        ignored.append(item)

    bc_raw = section("bc")
    ss4_raw = section("ss4")
    missing_raw = section("missing")

    spec = Spec(
        name=need(raw, "name", "top level"),
        description=raw.get("description", ""),
        source_of_truth=raw.get("source_of_truth", "both"),
        bc=BcSide(target=need(bc_raw, "target", "bc"),
                  api=bc_raw.get("api", "standard"),
                  entity=need(bc_raw, "entity", "bc"),
                  key=as_list(need(bc_raw, "key", "bc")),
                  filter=bc_raw.get("filter")),
        ss4=Ss4Side(database=ss4_raw.get("database"),
                    table=ss4_raw.get("table"),
                    query=ss4_raw.get("query"),
                    key=as_list(need(ss4_raw, "key", "ss4")),
                    where=ss4_raw.get("where")),
        missing_in_bc=missing_raw.get("in_bc", "error"),
        missing_in_ss4=missing_raw.get("in_ss4", "error"),
        fields=fields,
        ignored=ignored,
    )

    problems += validate(spec)
    if problems:
        raise SpecError(f"{label} has {len(problems)} problem(s):\n  - "
                        + "\n  - ".join(problems))
    return spec


def validate(spec: Spec) -> list[str]:
    """Checks that need the whole spec at once. Returns problems, never raises."""
    p = []

    # The name becomes a temp table (#bc_<name>) and a file name.
    if spec.name and not re.match(r"^\w+$", str(spec.name)):
        p.append(f"name: '{spec.name}' may only contain letters, digits and underscores")

    if spec.source_of_truth not in SOURCES_OF_TRUTH:
        p.append(f"source_of_truth: '{spec.source_of_truth}' must be one of {sorted(SOURCES_OF_TRUTH)}")
    if spec.bc.api not in BC_APIS:
        p.append(f"bc.api: '{spec.bc.api}' must be one of {sorted(BC_APIS)}")

    # The SS4 side: one table, or one query -- not both, not neither.
    if bool(spec.ss4.table) == bool(spec.ss4.query):
        p.append("ss4: give either 'table' (with 'database') or 'query', not both")
    if spec.ss4.table and not spec.ss4.database:
        p.append("ss4: 'database' is required with 'table'")
    if spec.ss4.query:
        q = str(spec.ss4.query).strip()
        if not re.match(r"^select\b", q, re.IGNORECASE):
            p.append("ss4.query: must be a single SELECT (no WITH)")
        if ";" in q or FORBIDDEN_SQL.search(q):
            p.append("ss4.query: read-only SELECT only; no semicolons, INTO, EXEC, DDL or DML")
    if spec.ss4.where and (";" in str(spec.ss4.where) or FORBIDDEN_SQL.search(str(spec.ss4.where))):
        p.append("ss4.where: a filter only; no semicolons, INTO, EXEC, DDL or DML")
    for label, level in (("missing.in_bc", spec.missing_in_bc), ("missing.in_ss4", spec.missing_in_ss4)):
        if level not in MISSING_LEVELS:
            p.append(f"{label}: '{level}' must be one of {sorted(MISSING_LEVELS)}")

    if len(spec.bc.key) != len(spec.ss4.key):
        p.append(f"key: bc has {len(spec.bc.key)} field(s) but ss4 has {len(spec.ss4.key)}; "
                 "they pair up by position, so the counts must match")

    if not spec.fields:
        p.append("fields: at least one field pair is needed")
    for f in spec.fields:
        if f.type not in FIELD_TYPES:
            p.append(f"field {f.bc} -> {f.ss4}: type '{f.type}' must be one of {sorted(FIELD_TYPES)}")
        if f.bc_blank not in BLANK_RULES:
            p.append(f"field {f.bc} -> {f.ss4}: bc_blank '{f.bc_blank}' must be one of {sorted(BLANK_RULES)}")

    for side in ("bc", "ss4"):
        names = [str(getattr(f, side)).lower() for f in spec.fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            p.append(f"fields: {side} column(s) mapped more than once: {dupes}")

        ignored = {str(i[side]).lower() for i in spec.ignored if i.get(side)}
        clash = sorted(ignored & set(names))
        if clash:
            p.append(f"ignored: {side} column(s) {clash} also appear in fields; "
                     "compare a column or ignore it, not both")

    for name in [spec.ss4.database, spec.ss4.table, *spec.ss4.key, *(f.ss4 for f in spec.fields)]:
        if name and not SAFE_NAME.match(str(name)):
            p.append(f"'{name}' contains characters not allowed in a SQL name")

    return p
