"""
Writes data rows into a BC configuration package template WITHOUT re-saving it
through an Excel library.

BC's package templates link their table to an XML map (xl/xmlMaps.xml): that is
how BC's import knows which column is which field. Excel libraries such as
openpyxl do not support XML maps, so saving through one drops the map while the
table still points at it -- Excel then reports the file as damaged, and BC's
import cannot read it.

So this edits the file the way the SOV script does: it opens the .xlsx as the
zip it is, replaces only the data rows below the table's header and the
table's size, and copies every other part (the map, connections, comments,
styles) through byte for byte.
"""

import posixpath
import re
import zipfile
from xml.sax.saxutils import escape

from openpyxl.utils import get_column_letter

# Characters XML 1.0 does not allow in text; Excel refuses files containing them.
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def attr(tag: str, name: str) -> str | None:
    """The value of one attribute in an XML start tag, whatever the attribute order."""
    match = re.search(rf'\s{re.escape(name)}="([^"]*)"', tag)
    return match.group(1) if match else None


def resolve(base_dir: str, target: str) -> str:
    """A relationship target as a path inside the zip: '../tables/table1.xml'
    seen from 'xl/worksheets' becomes 'xl/tables/table1.xml'."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def relationships(parts: dict, rels_path: str) -> list[tuple[str, str, str]]:
    """(Id, Type, Target) for every relationship in a .rels part."""
    xml = parts[rels_path].decode("utf-8")
    return [(attr(t, "Id"), attr(t, "Type") or "", attr(t, "Target"))
            for t in re.findall(r"<(?:\w+:)?Relationship\b[^>]*>", xml)]


def find_parts(parts: dict, sheet_title: str, table_name: str) -> tuple[str, str]:
    """The zip paths of the named sheet and of its named table."""
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    rid = None
    for tag in re.findall(r"<(?:\w+:)?sheet\b[^>]*>", workbook):
        if attr(tag, "name") == sheet_title:
            rid = attr(tag, "r:id")
    if rid is None:
        raise SystemExit(f"sheet '{sheet_title}' not found in the template")

    sheet_path = next(resolve("xl", target)
                      for i, _, target in relationships(parts, "xl/_rels/workbook.xml.rels") if i == rid)

    sheet_dir, sheet_file = posixpath.split(sheet_path)
    rels_path = f"{sheet_dir}/_rels/{sheet_file}.rels"
    for _, rel_type, target in relationships(parts, rels_path):
        if rel_type.endswith("/table"):
            table_path = resolve(sheet_dir, target)
            table_xml = parts[table_path].decode("utf-8")
            table_tag = re.search(r"<(?:\w+:)?table\b[^>]*>", table_xml).group(0)
            if attr(table_tag, "name") == table_name or attr(table_tag, "displayName") == table_name:
                return sheet_path, table_path
    raise SystemExit(f"table '{table_name}' not found on sheet '{sheet_title}'")


def build_rows(prefix: str, rows: list[list[str]], first_row: int, first_col: int) -> str:
    """The new <row> elements: every value as inline text, so codes like 07073
    keep their leading zeros and nothing is read as a number or a formula."""
    out = []
    for i, values in enumerate(rows):
        r = first_row + i
        cells = []
        for offset, value in enumerate(values):
            if value == "":
                continue
            ref = f"{get_column_letter(first_col + offset)}{r}"
            text = escape(ILLEGAL_XML.sub("", value))
            cells.append(f'<{prefix}c r="{ref}" t="inlineStr"><{prefix}is>'
                         f'<{prefix}t xml:space="preserve">{text}</{prefix}t></{prefix}is></{prefix}c>')
        out.append(f'<{prefix}row r="{r}">{"".join(cells)}</{prefix}row>')
    return "".join(out)


def write_table_rows(template_path, out_path, sheet_title: str, table_name: str,
                     header_row: int, first_col: int, last_col: int,
                     rows: list[list[str]]) -> None:
    """Copies the template to out_path with the table's data rows replaced by
    rows (one list of text values per row, in column order)."""
    with zipfile.ZipFile(template_path) as z:
        names = z.namelist()
        parts = {name: z.read(name) for name in names}

    sheet_path, table_path = find_parts(parts, sheet_title, table_name)
    last_row = header_row + max(len(rows), 1)
    first_letter, last_letter = get_column_letter(first_col), get_column_letter(last_col)

    # ---- the sheet: keep every row down to the header, then our rows
    sheet = parts[sheet_path].decode("utf-8")
    prefix = re.search(r"<(\w+:)?worksheet\b", sheet).group(1) or ""
    match = re.search(rf"<{prefix}sheetData\b[^>]*?(?:/>|>(.*?)</{prefix}sheetData>)", sheet, re.S)
    body = match.group(1) or ""
    kept = "".join(m.group(0) for m in re.finditer(rf"<{prefix}row\b[^>]*?(?:/>|>.*?</{prefix}row>)", body, re.S)
                   if int(attr(m.group(0), "r") or 0) <= header_row)
    new_data = f"<{prefix}sheetData>{kept}{build_rows(prefix, rows, header_row + 1, first_col)}</{prefix}sheetData>"
    sheet = sheet[:match.start()] + new_data + sheet[match.end():]

    # The sheet's used range, so Excel knows how far the data goes.
    def new_dimension(m):
        start = m.group(2).split(":")[0]
        return f'{m.group(1)}{start}:{last_letter}{last_row}{m.group(3)}'
    sheet = re.sub(rf'(<{prefix}dimension\b[^>]*\sref=")([^"]*)(")', new_dimension, sheet, count=1)
    parts[sheet_path] = sheet.encode("utf-8")

    # ---- the table: just its size, everything else (incl. the map links) unchanged
    span = f"{first_letter}{header_row}:{last_letter}{last_row}"
    table = parts[table_path].decode("utf-8")
    table = re.sub(r'(<(?:\w+:)?table\b[^>]*\sref=")[^"]*(")', rf"\g<1>{span}\g<2>", table, count=1)
    table = re.sub(r'(<(?:\w+:)?autoFilter\b[^>]*\sref=")[^"]*(")', rf"\g<1>{span}\g<2>", table, count=1)
    parts[table_path] = table.encode("utf-8")

    # ---- every part back, in the original order; only the two above changed
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            z.writestr(name, parts[name])
