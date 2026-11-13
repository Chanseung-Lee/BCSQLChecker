"""
Reads the files that built an import -- the population query or script, and
the filled-in config package template -- into text the mapping agent can read.

    python mapper.py jobPlanningLines --source sov_planning_lines.py --source SOV_PLANLINES_01.xlsx

Text files (.sql, .py, ...) are passed on as they are, up to a size limit.
Excel files are summarized: each sheet's table, its column headers and a few
sample rows -- enough to see which BC fields the import filled, without
sending thousands of rows.
"""

from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import range_boundaries

TEXT_SUFFIXES = {".sql", ".py", ".txt", ".md", ".cs", ".al", ".json", ".yaml", ".yml"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}

MAX_TEXT_CHARS = 60_000    # per text file; longer files are cut, and say so
SAMPLE_ROWS = 5            # data rows shown per table in an Excel summary
MAX_CELL_CHARS = 60        # characters shown per cell in an Excel summary


class SourceError(Exception):
    """A source file is missing or of a type we cannot read."""


def read_text_file(path: Path) -> str:
    """The file's text. SSMS can save .sql files as UTF-16, so that is
    recognized by its byte-order mark; everything else is read as UTF-8."""
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="replace")


def cell_text(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\n", " ").strip()
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS - 3] + "..."
    return text


def row_text(sheet, row: int, first_col: int, last_col: int) -> str:
    values = []
    for col in range(first_col, last_col + 1):
        values.append(cell_text(sheet.cell(row=row, column=col).value))
    return " | ".join(values)


def describe_range(sheet, ref: str, label: str) -> list[str]:
    """Header and sample rows of one block of cells, e.g. a config package table."""
    first_col, first_row, last_col, last_row = range_boundaries(ref)
    lines = []

    # Rows above the table: a BC package export puts the table's name and
    # number up there, which tells the agent which BC table this is.
    for row in range(1, first_row):
        text = row_text(sheet, row, first_col, last_col)
        if text.replace("|", "").strip():
            lines.append(f"  above the table, row {row}: {text}")

    lines.append(f"  {label} {ref}: {last_row - first_row:,} data rows")
    lines.append(f"  columns: {row_text(sheet, first_row, first_col, last_col)}")
    last_sample = min(last_row, first_row + SAMPLE_ROWS)
    for row in range(first_row + 1, last_sample + 1):
        lines.append(f"  row {row}: {row_text(sheet, row, first_col, last_col)}")
    return lines


def summarize_workbook(path: Path) -> str:
    workbook = load_workbook(path, data_only=True)
    lines = []
    for sheet in workbook.worksheets:
        lines.append(f"sheet '{sheet.title}'")
        if sheet.tables:
            # openpyxl versions differ: a table entry is either a Table or its range.
            for name, table in sheet.tables.items():
                ref = table.ref if hasattr(table, "ref") else table
                lines.extend(describe_range(sheet, ref, f"table '{name}'"))
        elif sheet.max_row > 1 or sheet.max_column > 1:
            lines.extend(describe_range(sheet, sheet.dimensions, "used range"))
        else:
            lines.append("  (empty)")
    return "\n".join(lines)


def read_source(path: Path) -> str:
    """One source file as a block of text for the agent's task prompt."""
    if not path.exists():
        raise SourceError(f"source file not found: {path}")

    suffix = path.suffix.lower()
    if suffix in EXCEL_SUFFIXES:
        body = summarize_workbook(path)
        kind = "Excel file, summarized"
    elif suffix in TEXT_SUFFIXES:
        body = read_text_file(path)
        kind = f"{suffix[1:]} file, {len(body):,} characters"
        if len(body) > MAX_TEXT_CHARS:
            body = body[:MAX_TEXT_CHARS] + f"\n... (cut here: the file has {len(body):,} characters)"
    else:
        supported = ", ".join(sorted(TEXT_SUFFIXES | EXCEL_SUFFIXES))
        raise SourceError(f"cannot read {path.name}: supported types are {supported}")

    return f"--- file: {path.name} ({kind}) ---\n{body}\n--- end of {path.name} ---"


def read_sources(paths: list[str]) -> str:
    """All source files, one block each. Empty text when there are none."""
    blocks = []
    for p in paths:
        blocks.append(read_source(Path(p)))
    return "\n\n".join(blocks)
