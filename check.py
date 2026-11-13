"""
Runs a spec and writes every finding to an Excel file for review.

    python check.py specs/partsUS.yaml
    python check.py specs/proposed/customers.yaml --proposed

No AI is involved: fetch the BC rows, stage them on SS4, compare in SQL, and
write the results. The same spec on the same data always gives the same file.

A spec still in specs/proposed/ has not been reviewed, so it is refused unless
--proposed is given -- and then the file is marked UNAPPROVED.

Exit code: 0 when there are no error-level findings, 1 when there are, 2 when
the check could not run. Handy if this is ever scheduled.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill

from connections import BcClient, BcTarget, ss4_engine
from diff import join_on, run_diff, ss4_scoped
from spec import load_spec
from stage import fetch_bc, stage_bc

RESULTS_DIR = Path("results")
PROPOSED_DIR = Path("specs/proposed")

# Findings are listed most serious first: by severity, then by kind.
SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}
KIND_ORDER = {
    "duplicate_in_ss4": 0,
    "duplicate_in_bc": 1,
    "missing_in_ss4": 2,
    "missing_in_bc": 3,
    "mismatch": 4,
}
KIND_DESCRIPTIONS = {
    "duplicate_in_ss4": "Key appears more than once in SS4",
    "duplicate_in_bc": "Key appears more than once in BC",
    "missing_in_ss4": "In BC, not in SS4",
    "missing_in_bc": "In SS4 (in scope), not in BC",
    "mismatch": "In both, but this field differs",
}

FONT = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
TITLE = Font(name="Arial", size=14, bold=True)
LINK = Font(name="Arial", size=10, color="0563C1", underline="single")
HEADER_FILL = PatternFill("solid", start_color="D9E1F2")

KEYS_SHOWN = 5   # keys listed per pile on the summary sheet


# ---------------------------------------------------------------- running the check

def is_proposed(spec_path: Path) -> bool:
    """True when the spec file sits inside specs/proposed/."""
    return PROPOSED_DIR.resolve() in spec_path.resolve().parents


def sort_key(finding):
    """Most serious first, then grouped by kind and field, then by key."""
    return (
        SEVERITY_ORDER.get(finding.severity, 9),
        KIND_ORDER.get(finding.kind, 9),
        finding.column or "",
        finding.key or "",
    )


def run_check(spec):
    """Fetches, stages and compares. Returns the findings plus the row counts."""
    print(f"fetching {spec.bc.entity} from BC ...")
    rows = fetch_bc(spec, BcClient())
    print(f"   {len(rows):,} rows")

    print("comparing on SS4 ...")
    with ss4_engine().connect() as conn:
        stage = stage_bc(conn, spec, rows)
        conn.commit()
        in_scope = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {ss4_scoped(spec)} s").scalar()
        paired = conn.exec_driver_sql(
            f"SELECT COUNT(*) FROM {stage} b JOIN {ss4_scoped(spec)} s ON {join_on(spec)}").scalar()
        findings = run_diff(conn, spec)

    counts = {"bc_rows": len(rows), "ss4_in_scope": in_scope, "paired": paired}
    return findings, counts


def side_effect_notes(findings) -> dict:
    """A mismatch on a key that is duplicated is probably caused by the
    duplicate (the extra row paired with the same BC record), not a real
    field difference. Returns {finding index: note} for those."""
    duplicate_keys = set()
    for f in findings:
        if f.kind.startswith("duplicate"):
            duplicate_keys.add((f.key or "").strip())

    notes = {}
    for i, f in enumerate(findings):
        if f.kind == "mismatch" and (f.key or "").strip() in duplicate_keys:
            notes[i] = "Key is duplicated (see the duplicate finding); probably a side effect"
    return notes


def group_findings(findings) -> list:
    """One entry per (severity, kind, field), in report order, with a count,
    the keys involved (up to KEYS_SHOWN) and the Findings-sheet row where the
    group starts. Used for the summary sheet and the console."""
    groups = []
    seen = {}
    for i, f in enumerate(findings):
        group_id = (f.severity, f.kind, f.column or "")
        if group_id not in seen:
            seen[group_id] = {"severity": f.severity, "kind": f.kind, "column": f.column or "",
                              "count": 0, "keys": [],
                              "first_row": i + 2}   # +2: Findings rows start below the header
            groups.append(seen[group_id])
        group = seen[group_id]
        group["count"] += 1
        if len(group["keys"]) < KEYS_SHOWN:
            group["keys"].append(f.key)
    return groups


def keys_text(group) -> str:
    """'12698923, 7465 043 01' -- or the first few and how many more."""
    shown = ", ".join(str(k) for k in group["keys"])
    if group["count"] > len(group["keys"]):
        shown += f", ... (+{group['count'] - len(group['keys']):,} more)"
    return shown


# ---------------------------------------------------------------- writing the workbook

def clean(value):
    """A value that is safe to put in a cell. Numbers pass through as numbers
    (writing them as text made Excel flag them). Text loses control characters,
    which Excel rejects."""
    if value is None or isinstance(value, (int, float)):
        return value
    return ILLEGAL_CHARACTERS_RE.sub("", str(value))


def write_cell(sheet, row, col, value, font=FONT):
    cell = sheet.cell(row=row, column=col, value=clean(value))
    if isinstance(cell.value, str) and cell.value.startswith("="):
        cell.data_type = "s"       # a value like '=ABC' stays text, not a formula
    cell.font = font
    return cell


def write_findings_sheet(sheet, findings, notes):
    headers = ["Severity", "Finding", "Field", "Key", "BC value", "SS4 value", "Note", "Reviewer note"]
    widths = [10, 18, 28, 22, 40, 40, 45, 40]

    for col, (header, width) in enumerate(zip(headers, widths), start=1):
        cell = write_cell(sheet, 1, col, header, BOLD)
        cell.fill = HEADER_FILL
        sheet.column_dimensions[cell.column_letter].width = width

    for i, f in enumerate(findings):
        row = i + 2
        write_cell(sheet, row, 1, f.severity)
        write_cell(sheet, row, 2, f.kind)
        write_cell(sheet, row, 3, f.column)
        write_cell(sheet, row, 4, f.key)
        write_cell(sheet, row, 5, f.bc_value)
        write_cell(sheet, row, 6, f.ss4_value)
        write_cell(sheet, row, 7, notes.get(i))
        # column 8, Reviewer note, is left empty for whoever checks the findings

    sheet.freeze_panes = "A2"
    last_row = max(len(findings) + 1, 2)
    sheet.auto_filter.ref = f"A1:H{last_row}"


def write_summary_sheet(sheet, spec, spec_path, proposed, counts, groups):
    target = BcTarget.from_env(spec.bc.target)
    if spec.ss4.table:
        ss4_source = f"{spec.ss4.database}.{spec.ss4.table}"
    else:
        ss4_source = "joined query (see the spec)"

    write_cell(sheet, 1, 1, f"Findings: {spec.name}", TITLE)
    if proposed:
        write_cell(sheet, 2, 1, "UNAPPROVED SPEC -- the mapping has not been reviewed yet", BOLD)

    info = [
        ("Spec file", str(spec_path)),
        ("Run at", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("BC", f"{target.environment} / {target.company} / {spec.bc.entity}"),
        ("BC filter", spec.bc.filter or "(none)"),
        ("SS4", ss4_source),
        ("SS4 scope", spec.ss4.where or "(none)"),
        ("BC rows", counts["bc_rows"]),
        ("SS4 rows in scope", counts["ss4_in_scope"]),
        ("Paired by key", counts["paired"]),
    ]
    row = 4
    for label, value in info:
        write_cell(sheet, row, 1, label, BOLD)
        write_cell(sheet, row, 2, value)
        row += 1

    # The groups table. Counts are COUNTIFS formulas over the Findings sheet, so
    # they stay right if a reviewer deletes rows they have dealt with.
    row += 1
    headers = ["Severity", "Finding", "Field", "Count", "Keys (click to see them)", "Meaning"]
    for col, header in enumerate(headers, start=1):
        cell = write_cell(sheet, row, col, header, BOLD)
        cell.fill = HEADER_FILL
    first_group_row = row + 1

    for group in groups:
        row += 1
        write_cell(sheet, row, 1, group["severity"])
        write_cell(sheet, row, 2, group["kind"])
        write_cell(sheet, row, 3, group["column"])
        if group["column"]:
            formula = (f'=COUNTIFS(Findings!$A:$A,A{row},Findings!$B:$B,B{row},'
                       f'Findings!$C:$C,C{row})')
        else:
            formula = f'=COUNTIFS(Findings!$A:$A,A{row},Findings!$B:$B,B{row})'
        sheet.cell(row=row, column=4, value=formula).font = FONT
        # The keys, linked to the first row of this pile on the Findings sheet.
        # Findings are sorted by pile, so the pile continues from there.
        keys_cell = write_cell(sheet, row, 5, keys_text(group), LINK)
        keys_cell.hyperlink = f"#Findings!A{group['first_row']}"
        write_cell(sheet, row, 6, KIND_DESCRIPTIONS.get(group["kind"], ""))

    row += 1
    write_cell(sheet, row, 3, "Total", BOLD)
    total = f"=SUM(D{first_group_row}:D{row - 1})" if groups else "=0"
    sheet.cell(row=row, column=4, value=total).font = BOLD

    row += 2
    legend = [
        "How to use: the Findings sheet has one row per finding, most serious first.",
        "Click a pile's keys to jump to its rows on the Findings sheet, where each",
        "row shows the BC and SS4 values side by side.",
        "Or filter the Findings sheet by Finding and Field to work through one pile at a time.",
        "Record what you found in the Reviewer note column.",
        "The counts above are formulas, so they update if you delete rows you have resolved.",
    ]
    for line in legend:
        write_cell(sheet, row, 1, line)
        row += 1

    for letter, width in zip("ABCDEF", [20, 20, 28, 10, 45, 34]):
        sheet.column_dimensions[letter].width = width


def write_workbook(path, spec, spec_path, proposed, counts, findings, notes, groups):
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    write_summary_sheet(summary, spec, spec_path, proposed, counts, groups)
    write_findings_sheet(workbook.create_sheet("Findings"), findings, notes)
    workbook.save(path)


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Compare a BC table with SS4 using a spec.")
    parser.add_argument("spec", help="path to the spec file, e.g. specs/partsUS.yaml")
    parser.add_argument("--proposed", action="store_true",
                        help="allow a spec that is still in specs/proposed/ (not yet reviewed)")
    args = parser.parse_args()

    spec_path = Path(args.spec)
    proposed = is_proposed(spec_path)
    if proposed and not args.proposed:
        print(f"{spec_path} is still in {PROPOSED_DIR}/, so it has not been reviewed.")
        print("Review it and move it into specs/, or add --proposed to run it anyway.")
        return 2

    spec = load_spec(spec_path)
    findings, counts = run_check(spec)

    findings.sort(key=sort_key)
    notes = side_effect_notes(findings)
    groups = group_findings(findings)

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    prefix = "UNAPPROVED_" if proposed else ""
    path = RESULTS_DIR / f"{prefix}{spec.name}_{stamp}.xlsx"
    write_workbook(path, spec, spec_path, proposed, counts, findings, notes, groups)

    print(f"\nBC rows {counts['bc_rows']:,}   SS4 rows in scope {counts['ss4_in_scope']:,}   "
          f"paired {counts['paired']:,}")
    print(f"{len(findings):,} finding(s):")
    for group in groups:
        field = f" / {group['column']}" if group["column"] else ""
        print(f"   {group['count']:>7,}  [{group['severity']}] {group['kind']}{field}")
    if notes:
        print(f"   ({len(notes):,} mismatch(es) are on duplicated keys, marked as likely side effects)")
    print(f"\n-> {path}")

    has_errors = any(f.severity == "error" for f in findings)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
