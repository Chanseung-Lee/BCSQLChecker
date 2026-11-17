"""
Writes fixed rows from a CSV into one sheet of a configuration package template:
for small setup tables (posting groups and the like) copied from another
company, rather than generated from SS4 the way fill.py does.

    python fill_static.py results\\Projects.xlsx --sheet "208 Project Posting Group" --csv setup\\project_posting_groups.csv
    python fill_static.py results\\Projects.xlsx --sheet "1006 Project WIP Method" --clear

The CSV's first row names template columns (the captions in the sheet's header
row). Template columns the CSV leaves out are written blank; a CSV column the
template does not have stops the run, so a typo cannot silently drop data.

Like fill.py, only that one sheet's data rows are replaced; every other sheet
and part of the file is kept exactly as it is. By default the file is updated in
place, so several setup sheets can be filled one after another; --out writes a
copy instead.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import range_boundaries

from xlsx_write import write_table_rows


def norm(name: str) -> str:
    """'Project Costs Applied Account' -> 'projectcostsappliedaccount', so a CSV header
    matches a template caption whatever its spacing or punctuation."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def main() -> int:
    parser = argparse.ArgumentParser(description="Write fixed rows from a CSV into one template sheet.")
    parser.add_argument("template", help="the package template (.xlsx) to update")
    parser.add_argument("--sheet", required=True, help="the sheet to fill, e.g. \"208 Project Posting Group\"")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="the rows to write; first row = template column captions")
    source.add_argument("--clear", action="store_true", help="remove the sheet's rows and write none")
    parser.add_argument("--out", help="write a copy here instead of updating the template in place")
    args = parser.parse_args()

    template_path = Path(args.template)
    out_path = Path(args.out) if args.out else template_path

    # ---- the sheet's table, header row and captions
    workbook = load_workbook(template_path)
    if args.sheet not in workbook.sheetnames:
        raise SystemExit(f"no sheet '{args.sheet}'; the template has: {', '.join(workbook.sheetnames)}")
    sheet = workbook[args.sheet]
    if not sheet.tables:
        raise SystemExit(f"sheet '{args.sheet}' has no Excel table")
    name, table = next(iter(sheet.tables.items()))
    table = sheet.tables[name]
    first_col, header_row, last_col, _ = range_boundaries(table.ref)
    captions = [str(sheet.cell(row=header_row, column=c).value or "").strip()
                for c in range(first_col, last_col + 1)]
    position = {norm(caption): i for i, caption in enumerate(captions)}

    # ---- the rows, in the template's column order
    rows = []
    if args.csv:
        with open(args.csv, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader)
            unknown = [h for h in header if norm(h) not in position]
            if unknown:
                raise SystemExit(f"CSV column(s) not in the '{args.sheet}' sheet: {unknown}\n"
                                 f"The sheet's columns are: {captions}")
            for record in reader:
                if not any(value.strip() for value in record):
                    continue                                  # skip blank lines
                values = [""] * len(captions)
                for h, value in zip(header, record):
                    values[position[norm(h)]] = value.strip()
                rows.append(values)

    write_table_rows(template_path, out_path, sheet.title, table.name,
                     header_row, first_col, last_col, rows)

    action = f"{len(rows)} row(s) written" if rows else "rows cleared"
    print(f"sheet '{args.sheet}': {action}")
    print(f"-> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
