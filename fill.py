"""
Fills a BC configuration package template from SS4, using a spec.

    python fill.py specs/customers.yaml templates/Customers.xlsx
    python fill.py specs/customers.yaml templates/Customers.xlsx --constants-from-bc
    python fill.py specs/customers.yaml templates/Customers.xlsx --constant "Shipping Agent Code=UPS"

A spec's SS4 side is a population query: its table or query, scoped by its
where filter, with one column per mapped BC field. This runs that query and
writes one template row per SS4 row, under the template's column headers.
After the import, check.py with the same spec verifies what BC ended up with.

How each template column gets its value, first match wins:
  1. --map "Caption=BC_Field"   links a header to a spec field when the names differ
  2. the spec                   a key or mapped field whose BC name matches the header
  3. --constant "Caption=Value" the same value on every row
  4. --constants-from-bc        the value BC holds on at least 90% of the spec's table's
                                rows (read from the BC company the spec points at, e.g. UAT)
  5. otherwise left blank, and listed in the summary

Nothing is cut short or guessed. Values longer than the template's character
limits, duplicate keys and blank keys are written as they are and listed in a
review file next to the output, so they can be fixed before importing.
"""

import argparse
import csv
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import range_boundaries

from check import is_proposed
from connections import BcClient, BcTarget, ss4_engine
from diff import ss4_scoped
from spec import load_spec
from stage import ENUM_ESCAPE, bc_url, profile_bc
from xlsx_write import write_table_rows

RESULTS_DIR = Path("results")

# "character limit 20", "(text 100 character limit)" -- how the template's
# description row states each column's maximum length.
LIMIT_RE = re.compile(r"(\d+)\s*character limit|character limit\s*(\d+)", re.IGNORECASE)


# A BC field counts as a constant when it holds ONE value on at least this share
# of rows. Not 100%: a few test or internal records with blanks would otherwise
# disqualify a posting group that every real customer shares. High enough that a
# sparse field (set on 3 customers) never spreads to all of them.
CONSTANT_MIN_SHARE = 0.90


# ---------------------------------------------------------------- names and values

def norm(name: str) -> str:
    """A name reduced to letters and digits, lowercase, so a template header and
    a BC field name can be matched: 'Phone No.' and 'Phone_No' both become
    'phoneno', 'Country/Region Code' and 'Country_Region_Code' both
    'countryregioncode'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def text_value(value) -> str:
    """A value as the text the template should hold."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        value = Decimal(str(value))
    if isinstance(value, Decimal):
        out = format(value.normalize(), "f")       # 16000.0000 -> "16000"
        return "0" if out in ("-0", "") else out
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    return str(value).strip()


def parse_pairs(items: list[str], option: str) -> dict:
    """['Caption=Value', ...] -> {normalized caption: (caption, value)}."""
    result = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"{option} needs the form \"Caption=Value\", got: {item}")
        caption, value = item.split("=", 1)
        result[norm(caption)] = (caption.strip(), value.strip())
    return result


# ---------------------------------------------------------------- the template

def find_table(workbook, sheet_name: str | None):
    """The sheet and Excel table that hold the template's data."""
    candidates = []
    for sheet in workbook.worksheets:
        if sheet_name and sheet.title != sheet_name:
            continue
        for name, table in sheet.tables.items():
            ref = table.ref if hasattr(table, "ref") else table
            candidates.append((sheet, sheet.tables[name], ref))
    if not candidates:
        raise SystemExit("no Excel table found in the template"
                         + (f" on sheet '{sheet_name}'" if sheet_name else ""))
    if len(candidates) > 1:
        sheets = ", ".join(sorted({s.title for s, _, _ in candidates}))
        raise SystemExit(f"the template has several tables (sheets: {sheets}); "
                         "choose one with --sheet")
    return candidates[0]


def column_limits(sheet, header_row: int, first_col: int, last_col: int) -> dict:
    """{column number: character limit}, read from the description rows above
    the header, where the template has them."""
    limits = {}
    for col in range(first_col, last_col + 1):
        for row in range(1, header_row):
            text = sheet.cell(row=row, column=col).value
            if not isinstance(text, str):
                continue
            match = LIMIT_RE.search(text)
            if match:
                limits[col] = int(match.group(1) or match.group(2))
                break
    return limits


# ---------------------------------------------------------------- the data

def sql_literal(text: str) -> str:
    """A fixed text value as a SQL literal: DOMESTIC -> N'DOMESTIC'."""
    return "N'" + text.replace("'", "''") + "'"


def template_sql(spec, plan) -> str:
    """One query shaped exactly like the template: one output column per template
    header, in order and under the header's name. Spec columns come from the spec's
    query, fixed values are written in as literals, and unfilled columns are NULL.
    This is the query fill.py runs, and the one saved next to the output."""
    columns = []
    for column in plan:
        name = "[" + column["caption"].replace("]", "]]") + "]"
        if column["source"] == "spec":
            columns.append(f"    s.[{column['ss4']}] AS {name}")
        elif column["source"] in ("constant", "bc"):
            columns.append(f"    {sql_literal(column['value'])} AS {name}")
        else:
            columns.append(f"    NULL AS {name}")

    order = ", ".join(f"s.[{k}]" for k in spec.ss4.key)
    return ("SELECT\n" + ",\n".join(columns) + "\n"
            f"FROM {ss4_scoped(spec)} s\n"
            f"ORDER BY {order};\n")


def constants_from_bc(spec) -> dict:
    """{normalized BC field name: (BC field name, value)} for every field that
    holds a single value on at least CONSTANT_MIN_SHARE of the spec's BC rows
    (within its bc.filter)."""
    target = BcTarget.from_env(spec.bc.target)
    rows = BcClient().fetch_all(bc_url(spec, target), filter=spec.bc.filter)
    if not rows:
        return {}

    fields = []
    for row in rows[:200]:
        for name, value in row.items():
            if not name.startswith("@odata") and not isinstance(value, (dict, list)) \
                    and name not in fields:
                fields.append(name)

    constants = {}
    for profile in profile_bc(rows, fields):
        if profile.distinct != 1 or profile.filled / profile.total < CONSTANT_MIN_SHARE:
            continue
        value = profile.top[0][0]
        # option values arrive escaped: 'One_x0020_customer' -> 'One customer'
        value = ENUM_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), value)
        if value.lower() == "true":
            value = "Yes"                      # the template's form for booleans
        constants[norm(profile.name)] = (profile.name, value)
    print(f"   BC constants: {len(constants)} field(s) hold one value on at least "
          f"{CONSTANT_MIN_SHARE:.0%} of {len(rows):,} rows")
    return constants


def plan_columns(captions, spec, maps, constants, bc_constants) -> list[dict]:
    """For each template column: where its value comes from."""
    # Normalized BC field name -> the SS4 column that feeds it.
    from_spec = {}
    for bc_name, ss4_name in zip(spec.bc.key, spec.ss4.key):
        from_spec[norm(bc_name)] = (bc_name, ss4_name)
    for f in spec.fields:
        from_spec[norm(f.bc)] = (f.bc, f.ss4)

    plan = []
    for caption in captions:
        lookup = norm(caption)
        if lookup in maps:                       # --map: this header is that BC field
            lookup = norm(maps[lookup][1])

        if lookup in from_spec:
            bc_name, ss4_name = from_spec[lookup]
            plan.append({"caption": caption, "source": "spec", "ss4": ss4_name,
                         "detail": f"{bc_name} <- SS4 {ss4_name}"})
        elif norm(caption) in constants:
            value = constants[norm(caption)][1]
            plan.append({"caption": caption, "source": "constant", "value": value,
                         "detail": f"'{value}' (--constant)"})
        elif lookup in bc_constants:
            bc_name, value = bc_constants[lookup]
            plan.append({"caption": caption, "source": "bc", "value": value,
                         "detail": f"'{value}' (BC {bc_name}, on 90%+ of UAT rows)"})
        else:
            plan.append({"caption": caption, "source": "blank", "detail": "left blank"})
    return plan


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Fill a BC config package template from SS4 using a spec.")
    parser.add_argument("spec", help="the spec file, e.g. specs/customers.yaml")
    parser.add_argument("template", help="the config package template (.xlsx) exported from BC")
    parser.add_argument("--out", help="output file (default: results/<template>_<spec>_<time>.xlsx)")
    parser.add_argument("--sheet", help="the sheet to fill, when the template has several")
    parser.add_argument("--map", action="append", default=[],
                        help='link a header to a spec field: --map "ZIP Code=Post_Code"')
    parser.add_argument("--constant", action="append", default=[],
                        help='fill a column with one value: --constant "Shipping Agent Code=UPS"')
    parser.add_argument("--constants-from-bc", action="store_true",
                        help="fill unmapped columns with values BC holds on every row of the spec's table")
    parser.add_argument("--proposed", action="store_true",
                        help="allow a spec still in specs/proposed/ (not yet reviewed)")
    args = parser.parse_args()

    spec_path = Path(args.spec)
    if is_proposed(spec_path) and not args.proposed:
        print(f"{spec_path} has not been reviewed yet. Review it and move it into specs/, "
              "or add --proposed to fill from it anyway.")
        return 2
    spec = load_spec(spec_path)
    maps = parse_pairs(args.map, "--map")
    constants = parse_pairs(args.constant, "--constant")

    # ---- the template
    template_path = Path(args.template)
    workbook = load_workbook(template_path)
    sheet, table, ref = find_table(workbook, args.sheet)
    first_col, header_row, last_col, _ = range_boundaries(ref)
    captions = [str(sheet.cell(row=header_row, column=c).value or "").strip()
                for c in range(first_col, last_col + 1)]
    limits = column_limits(sheet, header_row, first_col, last_col)
    print(f"template: {template_path.name}, sheet '{sheet.title}', {len(captions)} columns")

    # ---- decide each column's source, then build the one query that fills them all
    bc_constants = constants_from_bc(spec) if args.constants_from_bc else {}
    plan = plan_columns(captions, spec, maps, constants, bc_constants)
    sql = template_sql(spec, plan)

    print("reading SS4 ...")
    with ss4_engine().connect() as conn:
        rows = conn.exec_driver_sql(sql).fetchall()     # one tuple per row, in plan order
    print(f"   {len(rows):,} rows in scope")

    # Which template columns hold the key, for naming rows in the review file.
    key_positions = [i for i, column in enumerate(plan)
                     if column["source"] == "spec" and column["ss4"] in spec.ss4.key]

    # ---- turn the rows into text, checking each value as it goes
    review = []
    seen_keys = {}
    text_rows = []
    for i, row in enumerate(rows):
        if key_positions:
            key = " | ".join(text_value(row[p]) for p in key_positions)
        else:
            key = f"row {i + 1}"

        if not key.replace("|", "").strip():
            review.append({"key": key, "column": "", "value": "", "issue": "blank key"})
        seen_keys[key] = seen_keys.get(key, 0) + 1

        values = []
        for offset, column in enumerate(plan):
            value = text_value(row[offset])
            values.append(value)

            limit = limits.get(first_col + offset)
            if limit and len(value) > limit:
                review.append({"key": key, "column": column["caption"], "value": value,
                               "issue": f"{len(value)} characters; the limit is {limit}"})
        text_rows.append(values)

    for key, count in seen_keys.items():
        if count > 1:
            review.append({"key": key, "column": "", "value": "",
                           "issue": f"key appears {count} times; the import needs it unique"})

    # ---- save: the template with only its data rows replaced (see xlsx_write.py:
    # re-saving through openpyxl would drop BC's XML map and damage the file)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = Path(args.out) if args.out else RESULTS_DIR / f"{template_path.stem}_{spec.name}_{stamp}.xlsx"
    write_table_rows(template_path, out_path, sheet.title, table.name,
                     header_row, first_col, last_col, text_rows)

    # With --sheet, the side files carry the sheet's name, so filling several sheets of
    # one package into the same file keeps each sheet's query and review separate.
    side_suffix = "_" + re.sub(r"\W+", "_", args.sheet).strip("_") if args.sheet else ""
    review_path = out_path.with_name(out_path.stem + side_suffix + "_review.csv")
    with review_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["key", "column", "value", "issue"])
        writer.writeheader()
        writer.writerows(review)

    # The exact query that produced this file, so every run can be traced and rerun.
    query_path = out_path.with_name(out_path.stem + side_suffix + "_query.sql")
    query_path.write_text(
        f"-- Query that filled {out_path.name}\n"
        f"-- Template: {template_path.name} (sheet '{sheet.title}')\n"
        f"-- Spec:     {spec_path}\n"
        f"-- Run:      {datetime.now():%Y-%m-%d %H:%M}, {len(rows):,} rows\n"
        f"--\n"
        f"-- This is exactly what fill.py ran; one output column per template column, in order.\n"
        f"-- Running it in SSMS shows the same data. fill.py writes every value as text, with\n"
        f"-- decimals trimmed (16000.0000 -> 16000), dates as yyyy-mm-dd and true/false as Yes/No.\n\n"
        + sql, encoding="utf-8")

    # ---- summary
    print("\ncolumns:")
    for column in plan:
        print(f"   {column['caption']:<28} {column['detail']}")
    blank = [c["caption"] for c in plan if c["source"] == "blank"]
    if blank:
        print(f"\n{len(blank)} column(s) left blank. Fill them with --constant, link them to a spec "
              "field with --map, or add them to the spec.")
    print(f"\n{len(rows):,} row(s) written, {len(review):,} issue(s) for review")
    print(f"-> {out_path}")
    print(f"-> {review_path}")
    print(f"-> {query_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
