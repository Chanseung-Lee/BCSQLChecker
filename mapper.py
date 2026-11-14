"""
Piece C: starts a Claude agent that works out, on its own, how a BC table maps
to SS4, and writes that mapping as a spec.

    python mapper.py items
    python mapper.py items --hint "the parts master is in IMData"   (optional pointer)
    python mapper.py salesOrders --source so_population.sql          (how the import was built)

The agent gets the BC field profile, the three tools in agent_tools.py, and
the instructions below -- nothing else. No built-in Claude Code tools (no
shell, no files, no web), none of your Claude Code settings or connectors, and
no existing spec. Its proposed spec lands in specs/proposed/ for review, next
to a log of every query it ran and everything it said.
"""

import argparse
import asyncio
import os
import re

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock,
                              UserMessage, create_sdk_mcp_server)

from agent_tools import PROPOSED_DIR, MappingSession, build_tools, profile_text
from connections import BcClient, BcTarget, ss4_engine
from sources import SourceError, read_sources
from stage import fetch_bc_raw, profile_bc, stage_bc_raw

# The databases the agent is told it may search (the read-only login decides
# what it CAN read; this decides where it looks).
DATABASES = [d.strip() for d in
             os.getenv("SS4_DATABASES", "SAPData,EQData,EQDataCor,ARData,IMData").split(",")]

TOOL_NAMES = ["run_sql", "check_spec", "submit_spec"]


SYSTEM_PROMPT = """\
You map a Business Central (BC) table to the SS4 SQL Server data it corresponds
to, and write the mapping as a spec. A diff engine uses the spec to compare the
two sides row by row. A person reviews your spec before it is used, so be
accurate and honest about uncertainty rather than complete-looking.

# Tools

- run_sql: read-only SELECTs on SS4. #bc_raw holds the whole BC table, one
  nvarchar column per BC field, named as in BC, with values exactly as BC sent
  them (surrounding spaces and line breaks included). Reach databases by
  three-part name. Results are capped, so ask questions with aggregates
  (counts, match rates), not by listing rows.
- check_spec: runs the real diff with a draft spec and reports how many rows
  paired by key, what is missing or duplicated, and how often each field agrees,
  with examples. This is how you prove a mapping.
- submit_spec: hand in the final spec with notes for the reviewer. Once, at the end.

# How to work

1. Find the SS4 table. Do not trust names; test values. List candidate tables
   from each database's INFORMATION_SCHEMA.COLUMNS, then check which column's
   values overlap BC's identifying field (usually `number` or `code`).
   Distinctive values (part numbers, names) are evidence. Common values (0, 'EA',
   'US', blanks) are not.
2. Pin down the key: the SS4 column(s) matching BC's identifying field for nearly
   every row. Check whether the key is unique on both sides. Everything else
   depends on rows pairing correctly.
   Watch for keys the import GENERATED, such as line numbers counted up per
   document. SS4 does not hold them, and recomputing them (ROW_NUMBER) only
   works while SS4 is unchanged: one item added or removed in SS4 shifts every
   later number, so the rows after it pair with the wrong partners and show as
   false differences. Prefer a natural key built from content (document +
   account + description, say). Fall back to recomputed numbering only when no
   natural key is unique, and say so in the notes.
3. Find the scope. If the SS4 table holds more than this BC table covers (several
   companies, inactive or historical rows), find the filter that makes the two
   populations line up -- for example, group matched vs unmatched SS4 rows by a
   candidate column. That filter becomes ss4.where. The filter says which rows
   BELONG to this BC table (a company, a status); it must never remove rows to
   hide data problems such as duplicates, blank keys or unsynced rows. Those are
   findings the diff exists to report -- leave them in and describe them in the
   notes.
   The BC side can be narrowed the same way with bc.filter, an OData $filter
   over the BC field names in #bc_raw -- for example Job_Task_No eq '9500' and
   Line_Type eq 'Billable' (text in single quotes, numbers bare). Use it when the
   BC table holds more than this import or sync produced: other imports, rows
   entered by hand. Same rule: it says which rows belong, never hides problems.
   check_spec re-fetches from BC with the filter, so a filter BC cannot parse
   shows up there as an error.
4. Map the fields. For each BC field in use, find the SS4 column holding the same
   values on paired rows. Measure; do not guess. Values may be transformed
   (trimmed, case, truncated, rounded, codes). Note any you rely on. A field
   missing from the main table may live in a related table -- one sharing the
   key, or one linked through a code on the main table. Look there before
   concluding a field has no counterpart.
5. Test with check_spec, read the report, refine, test again.
6. Submit.

# Judgment

- A mapping is established by agreement on distinctive values, never by names alone.
- Your job is an honest mapping, not a clean report. A spec that hides real
  problems to make the numbers look good is worse than one that shows them.
- When you explain a difference (in notes or ignored reasons), say what you
  verified and what you are inferring. Do not state a cause you have not checked.
- Your task is the mapping, not root causes. Do not investigate WHY records are
  missing or different. Describe patterns that help a person check them (which
  records, number ranges, dates, what they have in common), then move on.
- When agreement is high (roughly 95%+ on distinctive values), map the field even
  if some rows differ. Those differences are findings; finding them is the
  point of the tool. Do not drop a correct mapping because the data has errors.
- When agreement is moderate, map the field only if you can explain the
  differences convincingly, and explain them in the notes. Otherwise leave it
  unmapped and say why.
- Tell "wrong column" apart from "right column, values deliberately differ" (for
  example a cost that BC recalculates). The second goes under ignored with the
  reason, so nobody mistakes it for a problem later.
- Read check_spec closely: a field compared on few rows is weak evidence; when
  one value dominates a field, agreement on the OTHER rows is the real evidence.
- BC fields ending in `Id` that hold GUIDs of BC's own lookup records (category,
  tax group, unit of measure...) do not exist in SS4; map the matching `Code`
  field instead. The record's own `id` may be stored in SS4 (look for a GUID
  column) -- check before dismissing it.
- The field profile marks each BC field:
    empty / constant: do not map; list under ignored (bc:) as "empty in BC" or
      "constant in BC".
    sparse: map only if agreement holds on enough of the filled rows.
    used: map it, or list it under ignored with why no counterpart was found.
  Every BC field must end up in fields or in ignored.
- List an SS4 column under ignored (ss4:) only when it looks like a counterpart
  but is deliberately not compared.
- bc_blank: ignore when a blank (or 0) in BC should not count as a difference --
  typically when BC is consistently blank where SS4 has a value. Otherwise compare.
- missing.in_bc / missing.in_ss4: info when unmatched rows on that side are
  expected (SS4 keeps extra history, say), error when they mean data did not
  arrive.
- source_of_truth: bc if SS4 looks like a copy of BC (it stores BC ids or sync
  timestamps), ss4 if BC was loaded from SS4, both if unclear.
- If several tables match (an original and a replica, say), choose the one that
  agrees best and name the other in the notes.

# Spec format

YAML. An example for a different table, to show the shape:

name: customers
description: BC customers vs the customer master
source_of_truth: ss4
bc:
  target: BC
  api: standard
  entity: customers
  key: [number]
ss4:
  database: ExampleDB
  table: dbo.Customer
  key: [CustomerID]
  where: Inactive = 0
missing:
  in_bc: info
  in_ss4: error
fields:
  - bc: displayName
    ss4: Name
    type: text
  - bc: balanceDue
    ss4: Balance
    type: decimal
    bc_blank: ignore
ignored:
  - bc: blocked
    reason: empty in BC
  - bc: currencyId
    reason: GUID of a BC lookup record; currencyCode is mapped instead

Field types: text, enum, decimal, int, bool, date, datetime, guid. #bc_raw holds
text, so choose the type from what the values are; the diff compares typed
values (decimal compares numerically, so 1.50 equals 1.5). Use enum for BC
option fields (status, type...): BC escapes characters in them as _xHHHH_
('In_x0020_Review'), and enum decodes that ('In Review') before comparing.
Text and enum comparisons ignore surrounding spaces, tabs and line breaks on
both sides, and treat blank as NULL. Datetimes compare to the millisecond.

# When fields come from several SS4 tables

If one table holds everything, use database + table as above. If some BC fields
come from other tables, use ss4.query instead: one SELECT that joins them and
names every output column. key, fields and where then refer to the QUERY's
output column names. For example:

ss4:
  query: >-
    SELECT c.CustomerID, c.Name, c.Inactive, s.ProjectEmail1 AS Email,
           b.SalesmanOne AS Salesperson
      FROM EQData.dbo.Customer c
      LEFT JOIN EQData.dbo.CustSupp s ON s.CustomerID = c.CustomerID
      LEFT JOIN EQDataCor.dbo.BillingInstructions b ON b.CustomerID = c.CustomerID
  key: [CustomerID]
  where: Inactive = 0

Rules for the query:
- A single SELECT, read-only, three-part table names. No WITH, no ORDER BY, no
  semicolons. Give every output column a unique name (alias duplicates).
- Start from the main table and LEFT JOIN the others, so a main row with no
  match in a side table still appears (with blanks) instead of vanishing.
- A join must not multiply rows: each record should appear once. The
  duplicate check in check_spec shows it if a join does.
- Computed columns are fine when that is how SS4 holds the value
  (d.Units * d.Rate AS ContractAmount), and say so in the notes.
- Prefer the plain table form when one table is enough.

# Source files

The user may attach the files that built the import: the SQL query or script
that populated a config package template, and/or the template itself
(summarized as sheets, column headers and sample rows). When they are present:

- Treat them as strong evidence of INTENT: which tables, joins, filters and
  transformations were used, and which BC fields the import filled. Start from
  them instead of searching by name.
- They are not proof. The query may have changed since the import, SS4 data may
  have changed, and the import may have rejected or altered rows -- which is
  exactly what this check exists to find. Verify every field with check_spec.
  Where the data contradicts a source file, map what the data supports and
  report the contradiction in the notes.
- Where the population query can serve as ss4.query, adapt it: remove ORDER BY,
  rewrite WITH / CTEs as nested SELECTs, replace parameters with the values the
  script uses (or the data shows), and keep the column aliases. Say in the notes
  what you changed.
- Template column headers are BC field CAPTIONS ("No.", "Project Task No."),
  not API field names. Match them to #bc_raw's fields by meaning and values.
- Logic the spec cannot express (row numbering, rules applied in a script's own
  code) may make some fields impossible to compare exactly. Put those under
  ignored with the reason, or compare what you can and explain.
- A field the template fills that is empty in BC is a likely import failure,
  not an unused field: map it anyway so the diff reports it. The profile's
  "empty" rule does not apply to fields the source files say were filled.

# Notes for the reviewer (submit_spec)

For each mapped field, the agreement figure that supports it. For each judgment
call, the reason. Anything uncertain, and alternatives you considered and rejected.
"""


def task_prompt(args, row_count: int, profiles, sources_text: str) -> str:
    hint = f"\nA pointer from the user: {args.hint}\n" if args.hint else ""
    sources = ""
    if sources_text:
        sources = ("\nSource files from the user, showing how this import was built "
                   "(see 'Source files' in your instructions):\n\n" + sources_text + "\n")
    return f"""\
Map the BC table `{args.entity}` to SS4.

It has {row_count:,} rows, all staged in #bc_raw.

BC field profile ("real values" excludes blanks, zeros and false):
{profile_text(profiles)}

SS4 databases to search: {", ".join(DATABASES)}
{hint}{sources}
In the spec use: name: {args.name}, and in the bc section target: {args.target},
api: {args.api}, entity: {args.entity}. When you are confident, call submit_spec.
"""


# ---------------------------------------------------------------- progress output

def one_line(text: str, limit: int = 150) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def result_text(block: ToolResultBlock) -> str:
    content = block.content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def show(message, transcript: list[str]) -> None:
    """Prints the agent's progress and keeps a full transcript for the log."""
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock) and block.text.strip():
                print(f"\nClaude: {block.text.strip()}")
                transcript.append(f"\n=== Claude\n{block.text.strip()}")
            elif isinstance(block, ToolUseBlock):
                name = block.name.removeprefix("mcp__mapper__")
                detail = block.input.get("sql", "")
                print(f"  -> {name}" + (f": {one_line(detail)}" if detail else ""))
                transcript.append(f"\n=== {name}\n" + "\n".join(str(v) for v in block.input.values()))
    elif isinstance(message, UserMessage) and isinstance(message.content, list):
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                text = result_text(block)
                transcript.append(f"--- result\n{text}")
                lines = text.splitlines()
                for line in lines[:8]:
                    print(f"       {line}")
                if len(lines) > 8:
                    print(f"       ... ({len(lines) - 8} more lines in the log)")
    elif isinstance(message, ResultMessage):
        cost = getattr(message, "total_cost_usd", None)
        summary = (f"\nDone: {message.subtype}, {getattr(message, 'num_turns', '?')} turns"
                   + (f", ${cost:.2f}" if cost is not None else ""))
        print(summary)
        transcript.append(summary)


# ---------------------------------------------------------------- main

async def run(args) -> None:
    # Read the source files first, so a wrong path fails before anything is fetched.
    try:
        sources_text = read_sources(args.source)
    except SourceError as ex:
        raise SystemExit(str(ex))
    for path in args.source:
        print(f"source file: {path}")

    target = BcTarget.from_env(args.target)
    print(f"fetching {args.entity} from BC ({target.environment} / {target.company}) ...")
    client = BcClient()
    rows = fetch_bc_raw(client, target, args.api, args.entity)
    print(f"   {len(rows):,} rows")
    if not rows:
        raise SystemExit("BC returned no rows; nothing to map.")

    transcript: list[str] = []
    with ss4_engine().connect() as conn: #Open a connection to SS4, everything below uses this one connection, closes when finished automatically
        _, fields = stage_bc_raw(conn, rows)  #copy the BC-returned rows into a temporary table called #bc_raw on SS4, one column per BC field, under BC's own field names
        #stage_bc_raw returns the table's name, and the list of field names.
        conn.commit()   # #bc_raw must survive the rollback run_sql does after an error. the commit makes the temp table permanent for the rest of the connection
        profiles = profile_bc(rows, fields) #BC has many fields, plenty are unused, always blank, always zero, or always the same value, and thus irrelevant when comparing with same data in SS4
        #produces one FieldProfile object per field, 

        #MappingSession object holds all resources that CLaude will need, like ss4, how to reach BC, returned rows we got earlier, etc.
        session = MappingSession(conn, rows, target=args.target, api=args.api, entity=args.entity,
                                 client=client) 
        #build_tools returns a list of 3 different tool objects. These are small packages from Claude's agent SDK
        #bundling the function with its label and expected inputs. 
        server = create_sdk_mcp_server(name="mapper", tools=build_tools(session))
        #Claude-provided function that creates the server that offers the tools given to Claude.
        #It doesn't run Claude here, Anthropic runs Claude on their side.

        #ClaudeAgentOptions is the SDK's settings object, how the Claude session should behave.
        options = ClaudeAgentOptions(
            system_prompt=SYSTEM_PROMPT, 
            mcp_servers={"mapper": server},           #the tool bundle from earlier
            tools=[],                                  # no built-in tools at all for safety
            allowed_tools=[f"mcp__mapper__{t}" for t in TOOL_NAMES],
            permission_mode="dontAsk",                 # anything not allowed above is denied
            setting_sources=[],                        # ignore your Claude Code settings
            strict_mcp_config=True,                    # ignore other MCP servers and connectors
            max_turns=args.max_turns,
            max_budget_usd=args.budget,
            model=args.model,
        )

        #There are two separate prompts, playing different roles.
        #SYSTEM_PROMPT is a long fixed block of text, telling CLaude who it is and how to work.
        #this task_prompt builds a fresh message string for this particular run, from this run's data, using profiles, rows, etc.
        prompt = task_prompt(args, len(rows), profiles, sources_text)

        #The conversation loop.
        #transcript is a list of text that becomes the log file by the end, the first entry is the task prompt itself.
        transcript.append(f"=== task\n{prompt}")
        print(f"starting the agent (max {args.max_turns} turns, ${args.budget:.2f} budget) ...")

        #starts the session, SDK launches, configured with the ClaudeAgentOptions above. When the block ends, it shuts down.
        #Tool requests are what create the back-and-forth
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt) #send the task prompt
            async for message in client.receive_response(): #receives everything that happens, one message at a time. Essentially watch it work until it's done
                show(message, transcript) #print and record the received message
                #show() checks what kind of message arrived and handles each one differently.

    PROPOSED_DIR.mkdir(parents=True, exist_ok=True)
    log_path = PROPOSED_DIR / f"{args.name}_log.txt"
    log_path.write_text("\n".join(transcript), encoding="utf-8")

    print()
    if session.submitted:
        print(f"Proposed spec: {session.submitted}")
    else:
        print("No spec was submitted (out of turns or budget?).")
    print(f"Full log:      {log_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Let Claude map a BC table to SS4.")
    parser.add_argument("entity", help="BC entity, e.g. items")
    parser.add_argument("--target", default="BC", help=".env prefix of the BC company (default BC)")
    parser.add_argument("--api", default="standard", choices=["standard", "odata"])
    parser.add_argument("--name", help="spec name (default: the entity)")
    parser.add_argument("--hint", help="an optional pointer for the agent")
    parser.add_argument("--source", action="append", default=[],
                        help="a file that built the import: the population query or script, "
                             "or the filled-in template. Repeat for several files.")
    parser.add_argument("--model", help="Claude model (default: the SDK's default)")
    parser.add_argument("--max-turns", type=int, default=80)
    parser.add_argument("--budget", type=float, default=5.0, help="stop at this many USD")
    args = parser.parse_args()
    args.name = args.name or re.sub(r"\W", "_", args.entity)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
