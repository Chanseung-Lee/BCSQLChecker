"""
The tools the mapping agent works with. All of them share ONE SS4 connection,
the one #bc_raw was staged on, so every query can join BC's data against SS4
directly.

  run_sql      explore: read-only SELECTs against SS4 and #bc_raw
  check_spec   test a draft mapping by running the real diff with it
  submit_spec  hand in the final mapping for human review
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import tool
from sqlalchemy.engine import Connection

from diff import bc_is_blank, join_on, run_diff, ss4_scoped
from spec import FORBIDDEN_SQL, Spec, SpecError, parse_spec
from stage import FieldProfile, fetch_bc, stage_bc

MAX_ROWS = 50                 # rows a query may return to the agent
MAX_CELL = 80                 # characters per value shown
QUERY_TIMEOUT_SECONDS = 120
FEW_ROWS = 30                 # below this, agreement is weak evidence either way
DOMINANT_SHARE = 0.90         # one value on this share of rows: check the other rows too
PROPOSED_DIR = Path("specs/proposed")

# A second layer behind the read-only login (same rule specs are checked against).
FORBIDDEN = FORBIDDEN_SQL


@dataclass
class MappingSession:
    conn: Connection
    rows: list[dict]      # the BC rows with every field (also staged in #bc_raw)
    target: str           # .env prefix they came from, e.g. "BC"
    api: str
    entity: str
    submitted: Path | None = None
    log: list[str] = field(default_factory=list)   # every tool call, for review
    client: Any = None    # BcClient, for re-fetching when a draft has a bc.filter


def reply(text: str) -> dict[str, Any]:
    """The shape the SDK expects a tool to return."""
    return {"content": [{"type": "text", "text": text}]}


def error_text(ex: Exception) -> str:
    """The database's own message, without SQLAlchemy's long wrapper."""
    return str(getattr(ex, "orig", ex))[:1500]


def clip(value, limit: int = MAX_CELL) -> str:
    s = "NULL" if value is None else str(value)
    return s if len(s) <= limit else s[:limit - 3] + "..."


def format_rows(columns: list[str], rows: list, more: bool) -> str:
    lines = [" | ".join(columns)] + [" | ".join(clip(v) for v in r) for r in rows]
    if not rows:
        lines.append("(no rows)")
    if more:
        lines.append(f"(only the first {MAX_ROWS} rows shown; aggregate or filter instead)")
    return "\n".join(lines)


def profile_text(profiles: list[FieldProfile]) -> str:
    """The BC field profile as a table, for the agent's instructions."""
    lines = ["field | status | real values | distinct | most common values"]
    for p in profiles:
        top = ", ".join(f"{clip(v, 40)!r} x{n:,}" for v, n in p.top)
        lines.append(f"{p.name} | {p.status} | {p.pct:.1f}% | {p.distinct:,} | {top}")
    return "\n".join(lines)


def agree_pct(compared: int, differ: int) -> str:
    """Two decimals, so 2 differences in 26,000 rows don't round up to 100.0%."""
    return f"{100 * (compared - differ) / compared:.2f}%" if compared else "n/a"

#Checks whether Claude's draft mapping actually holds up against the real data.
#A draft spec has a set of claims, such as BC's number matches SS4's number, etc.
#This tests thsoe claims by applying the mapping to every row and measuring how well BC and SS4
#line up as a result.
#Important: evaluate runs while Claude is still working, in the middle of the mapping.
def evaluate(conn: Connection, spec: Spec, rows: list[dict], client=None) -> str:
    """Runs the real diff with a draft spec and summarizes it for the agent."""
    # A bc.filter narrows which BC rows belong. It is OData syntax, so BC itself
    # applies it: fetch again with the filter rather than reuse every staged row.
    # A filter BC cannot parse comes back as an error the agent can read.
    filter_note = ""

    # 1. If the spec's "filter" narrows the BC side, fetch just those rows from BC
    if spec.bc.filter:
        if client is None:
            raise RuntimeError("this draft has a bc.filter, but no BC client was provided")
        rows = fetch_bc(spec, client)
        filter_note = f" (after bc.filter: {spec.bc.filter})"

    # 2. Put BC's rows on SS4, shaped the way spec describes
    #This means the spec decides which columns the staging table gets, what they're called and what type each column is
    stage = stage_bc(conn, spec, rows)
    conn.commit()   # a later rollback must not drop the staged table
    pairs = f"FROM {stage} b JOIN {ss4_scoped(spec)} s ON {join_on(spec)}"

    #How many BC rows found an SS4 partner
    paired = conn.exec_driver_sql(f"SELECT COUNT(*) {pairs}").scalar()
    #Scope is equivalent to filter from BC's side, it's which SS4 rows are supposed to correspond to this BC table,
    #set by the spec's 'where' field.
    in_scope = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {ss4_scoped(spec)} s").scalar()
    #run_diff writes SQL that refers to both tables by name and the sql server does the actual pairing and comparing
    findings = run_diff(conn, spec)

    #below code is putting together a summary to send back to Claude from the run_diff output

    #Build a dictionary of piles, one pile her kind and field tuple. 
    groups: dict[tuple, list] = {}
    for f in findings:
        groups.setdefault((f.kind, f.column), []).append(f)

    # the headline
    lines = [f"BC rows: {len(rows):,}{filter_note}   SS4 rows in scope: {in_scope:,}   paired by key: {paired:,}"]

    # one line per kind, so one for duplicates in ss4, duplicates in bc, etc.
    for kind in ("duplicate_in_ss4", "duplicate_in_bc", "missing_in_ss4", "missing_in_bc"):
        items = groups.get((kind, None), [])
        if items:
            lines.append(f"{kind}: {len(items):,}   e.g. {', '.join(f.key for f in items[:5])}")

    #Now for mismatches. For every field logged in spec (meaning determined as used by Claude after looking at BC):
    lines.append("field agreement on paired rows:")
    for fm in spec.fields:
        if fm.ss4 in spec.ss4.key:
            continue
        col = f"b.[{fm.ss4}]"

        #Get this field's pile of mismatches from earlier groups dictionary, blank if it's not there
        items = groups.get(("mismatch", fm.ss4), [])

        # With bc_blank: ignore, rows where BC is blank are not compared at all,
        # so agreement is measured only over the rows that actually were.
        blank_filter = f" AND NOT ({bc_is_blank(col, fm)})" if fm.bc_blank == "ignore" else ""
        #compared runs a small query: how many paired rows, meaning records that matched by key between BC and SS4 (excluding blank-BC rows)?
        #so without the blank_filter, it gets the same number every time in this for loop
        #the field having bc_blank: ignore means 'when BC is blank on a row, don't count that as a difference.
        #scenarios for this would be, if BC stores 0 for 'not set', such as unit price
        compared = conn.exec_driver_sql(f"SELECT COUNT(*) {pairs} WHERE 1 = 1{blank_filter}").scalar()
        #So above translates to: Count the BC rows that have a partner in SS4, but only those where BC's value for the field isn't blank or zero. (assuming blank_filter is set to ignore)
        lines.append(f"  {fm.bc} -> {fm.ss4}: {len(items):,} differ of {compared:,} compared "
                     f"({agree_pct(compared, len(items))} agree)")
        if compared < paired:
            lines.append(f"      {paired - compared:,} paired rows not compared: BC blank")
        if compared < FEW_ROWS:
            lines.append("      few rows compared: weak evidence either way")

        # When one value dominates, high agreement is mostly agreement on that
        # one value, which a coincidence or a shared default would also give.
        # The rows holding the OTHER values are the real evidence.
        top = conn.exec_driver_sql(
            f"SELECT TOP 1 CAST({col} AS nvarchar(4000)), COUNT(*) {pairs} WHERE 1 = 1{blank_filter} "
            f"GROUP BY CAST({col} AS nvarchar(4000)) ORDER BY COUNT(*) DESC").first()
        if top and compared and top[1] / compared >= DOMINANT_SHARE:
            others = compared - top[1]
            other_diffs = sum(1 for m in items if m.bc_value != top[0])
            lines.append(f"      {top[1] / compared:.1%} of compared rows hold {clip(top[0], 40)!r}; "
                         f"on the other {others:,}: {other_diffs:,} differ "
                         f"({agree_pct(others, other_diffs)} agree)")

        for m in items[:3]:
            lines.append(f"      {m.key}: BC={m.bc_value!r}  SS4={m.ss4_value!r}")
    return "\n".join(lines)


def build_tools(session: MappingSession) -> list:
    conn = session.conn
    conn.connection.dbapi_connection.timeout = QUERY_TIMEOUT_SECONDS   # no runaway queries

    @tool("run_sql",
          "Run one read-only SELECT (or WITH ... SELECT) on SS4. #bc_raw holds the BC "
          "table: one nvarchar column per BC field, named as in BC. Reach any database "
          f"by three-part name (IMData.dbo.Parts). At most {MAX_ROWS} rows come back, "
          "so prefer aggregates (counts, match rates) over listing rows.",
          {"sql": str})
    async def run_sql(args):
        sql = args["sql"].strip().rstrip(";")
        session.log.append(f"run_sql: {sql}")
        if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
            return reply("Rejected: only SELECT or WITH ... SELECT queries are allowed.")
        if FORBIDDEN.search(sql) or ";" in sql:
            return reply("Rejected: one read-only query at a time; no INTO, EXEC, DDL or DML.")
        try:
            result = conn.exec_driver_sql(sql)
            try:
                columns = list(result.keys())
                fetched = result.fetchmany(MAX_ROWS + 1)
            finally:
                result.close()   # discard unread rows, or the connection stays busy
            return reply(format_rows(columns, fetched[:MAX_ROWS], len(fetched) > MAX_ROWS))
        except Exception as ex:
            conn.rollback()      # safe: the staged tables were committed
            return reply(f"SQL error: {error_text(ex)}")

    @tool("check_spec",
          "Validate a draft spec (YAML text) and, if valid, run the full diff with it. "
          "Returns the validation problems, or how many rows paired by key, what is "
          "missing or duplicated, and how often each field agrees, with examples. "
          "Use it to test a mapping before submitting.",
          {"spec_yaml": str})
    async def check_spec(args):
        session.log.append("check_spec:\n" + args["spec_yaml"])
        #first, turn the text into a Spec, and check it for mistakes
        try:
            #safe_load turns the yaml text into a Python dictionary, the PyYAML library doing the text parsing
            #parse_spec takes that dictionary and builds the Spec object out of it, the BcSide, the Ss4Side, a FieldMap for each field
            spec = parse_spec(yaml.safe_load(args["spec_yaml"]), "draft")
        except (SpecError, yaml.YAMLError) as ex:
            return reply(f"Not valid:\n{ex}")
        # make sure it's about the table being mapped, verify the spec's identity with the session's
        if (spec.bc.target, spec.bc.api, spec.bc.entity) != (session.target, session.api, session.entity):
            return reply(f"The bc section must be target: {session.target}, api: {session.api}, "
                         f"entity: {session.entity}, the table being mapped.")
        #run the comparison and report back
        try:
            #evaluate is the comparison engine.
            return reply(evaluate(conn, spec, session.rows, session.client))
        except Exception as ex:
            conn.rollback()
            return reply(f"Running the diff failed: {error_text(ex)}")

    @tool("submit_spec",
          "Submit the final spec (YAML text) plus notes for the human reviewer: the "
          "evidence behind each mapping, and anything uncertain. Call once, at the end.",
          {"spec_yaml": str, "notes": str})
    async def submit_spec(args):
        session.log.append("submit_spec")
        try:
            spec = parse_spec(yaml.safe_load(args["spec_yaml"]), "submitted")
        except (SpecError, yaml.YAMLError) as ex:
            return reply(f"Not valid, nothing saved:\n{ex}")
        PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
        path = PROPOSED_DIR / f"{spec.name}.yaml"
        notes = "\n".join(f"# {line}".rstrip() for line in args["notes"].splitlines())
        path.write_text(f"# PROPOSED by the mapping agent -- review before use.\n#\n{notes}\n\n"
                        f"{args['spec_yaml'].strip()}\n", encoding="utf-8")
        session.submitted = path
        return reply(f"Saved to {path}. Finish with a short summary.")

    return [run_sql, check_spec, submit_spec]
