"""
Turns each current contract's Schedule of Values (SAPData.dbo.maSOV /
maSOVDetails on ss4) into Business Central project planning lines, written
into the RENT-PLANLINES configuration package template (table 1003).

What one SOV item becomes (as decided: ROUTE = "billable", BILLABLE_AMOUNT = "full")
-----------------------------------------------------------------------------------
  One Billable line for the item's FULL contract value (Units x Rate), however
  much ss4 has already invoiced. Items already invoiced in ss4 are listed in
  the review file, since invoicing them again from BC would double-bill.
  Rent, repair and property tax get no line: the 2100/2200 schedule bills them.

Other settings, if the decision changes (ROUTE = "both")
--------------------------------------------------------
  Budget line    the item's contract value: Units x Rate (e.g. Freight In
                 1 x $6,000; Equipment Rental 45 months x $78,000). Never
                 invoiced; BC compares it with what is invoiced per task.
  Billable line  what is still left to bill: contract value - billed to date.
                 Billed to date is summed LIVE from the ss4 invoices with the
                 item's billing code (the rule ss4's sp_maSOVGetPaymentsData
                 uses) -- not the SOV's stored WorkFromPrevApp, which only
                 refreshes when the SOV screen runs and was found months stale.
                 Invoiced from BC with Create Sales Invoice.
                 None when nothing is left, or when ss4 marks the item not
                 billable.

ROUTE = "budget" writes only budget lines, "billable" only billable lines.

Rent, repair and property tax (BUDGET_ONLY_CODES) only ever get the budget
line: their billable schedule already exists in task 2100/2200 from the
rental invoice import, so a billable line here would bill them twice.

Which items
-----------
Priced items only: Units <> 0 and a contract value <> 0. The rest of an SOV is
a rate card (overtime rates, per diems, ...) with no contract value.

Which contracts
---------------
The same current contracts as the rent scripts (same AS_OF and rules), each
with its latest SOV. A contract with more than one SOV is reported.

Tasks
-----
Every SOV item goes into ONE task per project, SOV_TASK (9500 Schedule of
Values) -- except rent, repair and property tax, whose budget lines go into the
project's existing rent task (2100 / 2200) beside their billing schedule.
If SETUP_TEMPLATE_PATH points at a fresh RENT-SETUP export, the script writes
the SOV task for every project that lacks it to SOV_TASKS_<co>.xlsx -- import
and apply that through RENT-SETUP (table 1001 only) before the planning lines.

PROVISIONAL settings -- confirm before importing
------------------------------------------------
  G/L accounts          read from ss4's own billing code setup
                        (ARBilling.Account2), which uses the same numbers as
                        BC. GL_OVERRIDES changes a code's account; codes with
                        no ss4 account (ACT, "N/A") are skipped and listed.
  BILLABLE_DATE_BY_CODE when a billable line is due: erect items at erection,
                        dismantle items at dismantle, the rest at AS_OF. Never
                        before FIRST_BILLABLE_DATE.

Existing lines
--------------
New lines are numbered after the highest existing line in each project and
task, so nothing in BC is overwritten. A line identical to one already in BC
(same project, task, line type, account and description) is skipped, so a
rerun does not duplicate. The $0 placeholder lines already on older projects
are left alone.

Before importing
----------------
Use a FRESH export of RENT-PLANLINES as TEMPLATE_PATH (BC now holds the rent
lines too), and keep "Delete Table Records Before Processing" UNCHECKED.

Requires:  pip install pandas sqlalchemy pyodbc
Run:       python sov_planning_lines.py
"""

import re
import urllib.parse
import zipfile
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape, unescape

import pandas as pd
from sqlalchemy import create_engine, text

SERVER = "ss4"
DATABASE = "ARData"          # also reads EQData and SAPData
DRIVER = "ODBC Driver 17 for SQL Server"

ODBC = (
    f"DRIVER={{{DRIVER}}};"
    f"SERVER={SERVER};"
    f"DATABASE={DATABASE};"
    "Trusted_Connection=yes;"
    "TrustServerCertificate=yes;"
)

HERE = Path(__file__).resolve().parent

# Fresh Export to Excel of the RENT-PLANLINES package (table 1003).
TEMPLATE_PATH = HERE / "Default10_11_2026_13_10_54.xlsx"

# Fresh Export to Excel of the RENT-SETUP package (tables 18, 167, 1001).
# None = do not check tasks (lines for missing tasks will fail validation).
SETUP_TEMPLATE_PATH = HERE / "Default10_11_2026_13_09_44.xlsx"

# ---- Scope: identical to the rent scripts ---------------------------------------

COMPANY_CODE = "01"
AS_OF = date(2026, 10, 30)
REQUIRED_CONTRACT_STATUS = "A"
LAST_RENT_INVOICE_SINCE = date(2026, 9, 1)
RENT_CODE = "RNT"
TASK_BY_RENT_TYPE = {"R": "2100", "A": "2200"}

OUTPUT_PATH = HERE / f"SOV_PLANLINES_{COMPANY_CODE}.xlsx"
TASKS_OUTPUT_PATH = HERE / f"SOV_TASKS_{COMPANY_CODE}.xlsx"
REVIEW_PATH = HERE / f"SOV_PLANLINES_{COMPANY_CODE}_review.csv"
BATCH_SIZE = 1000

# ---- The route ----------------------------------------------------------------------

ROUTE = "billable"           # "billable", "budget" or "both"

# Amount on the billable line:
#   "full"       the item's whole contract value, whatever ss4 already billed
#                (as decided: one billable line per item, full amount)
#   "remaining"  contract value minus what ss4 already invoiced
BILLABLE_AMOUNT = "full"

# Billed through the rent schedule in 2100/2200: budget line only, ever.
BUDGET_ONLY_CODES = {"RNT", "RPR", "PTX", "ACT"}

# ---- Tasks ------------------------------------------------------------------------------

SOV_TASK = "9500"                       # unused on every project in BC
SOV_TASK_DESCRIPTION = "Schedule of Values"
RENT_SCHEDULE_CODES = BUDGET_ONLY_CODES  # these go in 2100/2200 instead
TASK_TYPE = "Posting"
TASK_POSTING_GROUP = "SETTING UP"

# ---- G/L accounts ------------------------------------------------------------------------
# Each item posts to the revenue account ss4 itself uses for its billing code
# (ARData.dbo.ARBilling.Account2: RNT 31105, FRI 34105, ANC 32105, ELSM 33120,
# OPR 33205, ...). BC uses the same account numbers. Put a code here only to
# post it somewhere else. Codes whose ss4 account is blank (ACT, "N/A") or
# listed in SKIP_CODES are skipped and listed in the review file.
GL_OVERRIDES = {}
SKIP_CODES = {"TXT"}     # Textura: ss4 posts it to 60510, an admin account, not revenue

# ---- Dates ----------------------------------------------------------------------------

# Budget lines: the SOV start date, else the contract's erection date, else AS_OF.
# Billable lines: the event the item belongs to, but never before this date.
FIRST_BILLABLE_DATE = AS_OF
_AT_ERECTION = ["ELSM", "ELST", "ELSWD", "ETR", "ETWE", "EPD", "LMPE", "BSLSM",
                "FRI", "FRL", "FRP", "ANC", "PRT"]
_AT_DISMANTLE = ["DLSM", "DLST", "DLSWD", "DSTR", "DPD", "LMPD", "FRO"]
BILLABLE_DATE_BY_CODE = {**{c: "erection" for c in _AT_ERECTION},
                         **{c: "dismantle" for c in _AT_DISMANTLE}}   # others: AS_OF

LINE_KIND = "G/L Account"
LINE_NO_STEP = 10000


SQL_SOV_ITEMS = """
    WITH RentInvoices AS (
        SELECT i.[Type], i.InvoiceDate, LTRIM(RTRIM(i.CustomerID)) AS CustomerID,
               ROW_NUMBER() OVER (PARTITION BY LTRIM(RTRIM(i.CustomerID))
                                  ORDER BY i.InvoiceDate DESC, i.InvoiceID DESC) AS rn
        FROM ARData.dbo.ARInvoice i WITH (NOLOCK)
        WHERE i.Company = :company_code AND i.[Type] IN ('R', 'A')
          AND i.ProcessStatus = 'FINISHED'
          AND EXISTS (SELECT 1 FROM ARData.dbo.ARInvoiceDetail x WITH (NOLOCK)
                      WHERE x.InvoiceID = i.InvoiceID AND x.Item = 1
                        AND x.Quantity <> 0 AND x.Code = :rent_code)
    ),
    Current_ AS (
        SELECT ri.CustomerID, ri.[Type] AS rent_type, c.ErectionDate, c.DismantleDate
        FROM RentInvoices ri
        JOIN EQData.dbo.Customer c WITH (NOLOCK) ON LTRIM(RTRIM(c.CustomerID)) = ri.CustomerID
        WHERE ri.rn = 1 AND ri.InvoiceDate >= :since
          AND c.DismantleDate > :as_of AND c.ContractStatus = :status
    ),
    Sovs AS (
        SELECT s.*, ROW_NUMBER() OVER (PARTITION BY LTRIM(RTRIM(s.ARID)) ORDER BY s.PKID DESC) AS sov_rank,
               COUNT(*) OVER (PARTITION BY LTRIM(RTRIM(s.ARID))) AS sov_count
        FROM SAPData.dbo.maSOV s WITH (NOLOCK)
    )
    SELECT cur.CustomerID                                   AS customer_id,
           cur.rent_type,
           cur.ErectionDate                                 AS erection_date,
           cur.DismantleDate                                AS dismantle_date,
           s.PKID                                           AS sov_id,
           s.sov_count,
           s.StartDate                                      AS sov_start,
           d.PKID                                           AS item_id,
           d.SOVItemNumber                                  AS item_no,
           LTRIM(RTRIM(ISNULL(d.WorkDesc, '')))             AS work_desc,
           LTRIM(RTRIM(ISNULL(d.BillAsCode, '')))           AS code,
           d.Units                                          AS units,
           ISNULL(d.UnitsDefinition, '')                    AS units_def,
           CAST(d.Rate AS decimal(19, 4))                   AS rate,
           CAST(ISNULL(d.ChangeOrdersTotal, 0) AS decimal(19, 4)) AS change_orders,
           CAST(d.NewContractAmount AS decimal(19, 4))      AS contract_amount,
           CAST(ISNULL(d.WorkFromPrevApp, 0) AS decimal(19, 4)) AS sov_billed,
           d.Billable                                       AS billable,
           LTRIM(RTRIM(ISNULL(b.Account2, '')))             AS ss4_gl
    FROM Current_ cur
    JOIN Sovs s ON LTRIM(RTRIM(s.ARID)) = cur.CustomerID AND s.sov_rank = 1
    JOIN SAPData.dbo.maSOVDetails d WITH (NOLOCK) ON d.SOVID = s.PKID
    LEFT JOIN ARData.dbo.ARBilling b WITH (NOLOCK)
      ON b.Company = :company_code AND b.Code = d.BillAsCode
    WHERE d.Units <> 0 AND ISNULL(d.NewContractAmount, 0) <> 0
    ORDER BY cur.CustomerID, d.SOVItemNumber
"""


# Billed to date, live from the invoices, in ONE pass: the rule ss4's own
# sp_maSOVGetPaymentsData uses (same customer, same billing code, invoice line
# tied to this SOV item or to none), but current. The SOV's stored
# WorkFromPrevApp only refreshes when someone opens the SOV screen.
SQL_INVOICED = """
    SELECT LTRIM(RTRIM(i.CustomerID))            AS customer_id,
           LTRIM(RTRIM(x.Code))                  AS code,
           ISNULL(x.SOVDetailPKID, 0)            AS item_id,
           CAST(SUM(x.Total) AS decimal(19, 4))  AS amount
    FROM ARData.dbo.ARInvoice i WITH (NOLOCK)
    JOIN ARData.dbo.ARInvoiceDetail x WITH (NOLOCK) ON x.InvoiceID = i.InvoiceID
    JOIN ARData.dbo.ARBilling b WITH (NOLOCK) ON b.Company = i.Company AND b.Code = x.Code
    WHERE i.Company = :company_code AND i.Interfaced = 1
    GROUP BY LTRIM(RTRIM(i.CustomerID)), LTRIM(RTRIM(x.Code)), ISNULL(x.SOVDetailPKID, 0)
"""


def load_sov_items(conn):
    params = {"company_code": COMPANY_CODE, "rent_code": RENT_CODE, "as_of": AS_OF,
              "since": LAST_RENT_INVOICE_SINCE, "status": REQUIRED_CONTRACT_STATUS}
    print("   querying SOV items ...")
    items = pd.read_sql(text(SQL_SOV_ITEMS), conn, params=params)
    print(f"   {len(items)} priced SOV item(s); querying invoice totals ...")
    invoiced = pd.read_sql(text(SQL_INVOICED), conn, params={"company_code": COMPANY_CODE})
    return normalize_items(add_billed(items, invoiced))


def add_billed(items, invoiced):
    """Per item: invoiced amount with its code, tied to it or to no item."""
    totals = {}
    for row in invoiced.itertuples(index=False):
        key = (str(row.customer_id).strip(), str(row.code).strip())
        totals.setdefault(key, {})[int(row.item_id or 0)] = \
            totals.get(key, {}).get(int(row.item_id or 0), 0) + float(row.amount or 0)
    billed = []
    for row in items.itertuples(index=False):
        by_item = totals.get((str(row.customer_id).strip(), str(row.code).strip()), {})
        billed.append(by_item.get(int(row.item_id), 0) + by_item.get(0, 0))
    items = items.copy()
    items["billed"] = billed
    return items


def normalize_items(df):
    df = df.copy()
    for column in ("erection_date", "dismantle_date", "sov_start"):
        df[column] = pd.to_datetime(df[column], errors="coerce").dt.date
    if "sov_billed" not in df:
        df["sov_billed"] = df["billed"]
    for column in ("rate", "change_orders", "contract_amount", "billed", "sov_billed"):
        df[column] = df[column].map(lambda v: Decimal(str(v or 0)).quantize(Decimal("0.01")))
    if "ss4_gl" not in df:
        df["ss4_gl"] = ""
    for column in ("customer_id", "rent_type", "work_desc", "code", "units_def", "ss4_gl"):
        df[column] = df[column].fillna("").astype(str).str.strip()
    df["units"] = df["units"].astype(int)
    df["billable"] = df["billable"].astype(bool)
    df["sov_count"] = df["sov_count"].astype(int)
    return df


# ---- Building the lines ----------------------------------------------------------------


def money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def note(review, row, issue, detail=""):
    review.append(dict(project=row.customer_id, sov_item=row.item_no, code=row.code,
                       description=row.work_desc, contract=str(row.contract_amount),
                       billed=str(row.billed), sov_screen_billed=str(row.sov_billed),
                       issue=issue, detail=detail))


def present(day):
    return day if isinstance(day, date) and not pd.isna(day) else None


def budget_line(row):
    """Units x Rate, or one line for the whole value when change orders apply."""
    if row.change_orders == 0 and row.units > 0:
        return Decimal(row.units), money(row.rate)
    return Decimal(1), money(row.contract_amount)


def billable_line(row, remaining):
    """Remaining balance; in whole units at the item's rate when it divides evenly."""
    if row.rate > 0 and remaining % row.rate == 0:
        return Decimal(int(remaining / row.rate)), money(row.rate)
    return Decimal(1), money(remaining)


def billable_date(row):
    rule = BILLABLE_DATE_BY_CODE.get(row.code, "as_of")
    event = {"erection": present(row.erection_date),
             "dismantle": present(row.dismantle_date)}.get(rule) or AS_OF
    return max(event, FIRST_BILLABLE_DATE), event


def build_lines(items, review):
    lines = []
    for row in items.itertuples(index=False):
        if row.code in SKIP_CODES:
            note(review, row, "not loaded: code in SKIP_CODES")
            continue
        gl = GL_OVERRIDES.get(row.code) or row.ss4_gl
        if not gl:
            note(review, row, "not loaded: no G/L account for this code in ss4")
            continue
        if row.code in RENT_SCHEDULE_CODES:
            task = TASK_BY_RENT_TYPE.get(row.rent_type)
            if not task:
                note(review, row, "not loaded: unknown rent type", row.rent_type)
                continue
        else:
            task = SOV_TASK

        base = dict(project=row.customer_id, task=task, gl=gl, code=row.code,
                    description=row.work_desc[:100], item_no=row.item_no)

        if ROUTE in ("both", "budget"):
            quantity, price = budget_line(row)
            day = present(row.sov_start) or present(row.erection_date) or AS_OF
            lines.append(dict(base, line_type="Budget", date=day,
                              quantity=quantity, price=price))

        if ROUTE in ("both", "billable"):
            remaining = money(row.contract_amount - row.billed)
            amount = money(row.contract_amount) if BILLABLE_AMOUNT == "full" else remaining
            if row.code in BUDGET_ONLY_CODES:
                if ROUTE == "billable":
                    note(review, row, "not loaded: billed by the 2100/2200 rent schedule")
            elif not row.billable:
                note(review, row, "no billable line: ss4 marks the item not billable")
            elif amount <= 0:
                if BILLABLE_AMOUNT == "remaining" and remaining < 0:
                    note(review, row, "no billable line: billed over contract", f"over by {-remaining}")
            else:
                if BILLABLE_AMOUNT == "full" and row.billed > 0:
                    note(review, row, "already invoiced in ss4: billing this line again would double-bill",
                         f"ss4 invoiced {row.billed} of {row.contract_amount}")
                quantity, price = billable_line(row, amount)
                day, event = billable_date(row)
                if event < FIRST_BILLABLE_DATE and row.billed == 0:
                    note(review, row, "check before billing: event already passed, still unbilled",
                         f"{BILLABLE_DATE_BY_CODE.get(row.code, 'as_of')} {event}; dated {day}")
                lines.append(dict(base, line_type="Billable", date=day,
                                  quantity=quantity, price=price))
    return lines


# ---- Templates -----------------------------------------------------------------------

ROW = re.compile(r'<(\w+:)?row\b[^>]*\br="(\d+)"[^>]*>(.*?)</\1row>', re.S)
CELL = re.compile(r'<(\w+:)?c\b[^>]*\br="([A-Z]+)\d+"[^>]*?(?:/>|>(.*?)</\1c>)', re.S)
TEXT = re.compile(r"<(?:\w+:)?t\b[^>]*>(.*?)</(?:\w+:)?t>", re.S)
CAPTIONS = {"Job No.": "Project No.", "Job Task No.": "Project Task No.",
            "Job Posting Group": "Project Posting Group", "Job Task Type": "Project Task Type"}


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def col_letters(index):
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def read_workbook(path):
    """Every sheet of a BC package export: its table, captions and rows."""
    if not path.exists():
        raise SystemExit(f"template not found: {path}")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        parts = {name: z.read(name) for name in names}
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    targets = {rid: t.lstrip("/") for t, rid in re.findall(r'Target="([^"]+)"\s+Id="([^"]+)"', rels)}
    sheets = []
    for title, rid in re.findall(r'<(?:\w+:)?sheet\b[^>]*name="([^"]*)"[^>]*r:id="([^"]*)"', workbook):
        sheet_part = targets[rid]
        sheet_rels = parts[sheet_part.replace("worksheets/", "worksheets/_rels/") + ".rels"].decode("utf-8")
        table_part = next(t.lstrip("/") for t in re.findall(r'Target="([^"]+)"', sheet_rels)
                          if re.search(r"/tables/table\d+\.xml$", t))
        table = parts[table_part].decode("utf-8")
        ref = re.search(r'<(?:\w+:)?table\b[^>]*\sref="([A-Z]+)(\d+):[A-Z]+\d+"', table)
        captions = [CAPTIONS.get(unescape(c), unescape(c)) for c in
                    re.findall(r'<(?:\w+:)?tableColumn\b[^>]*\sname="([^"]*)"', table)]
        letters = [col_letters(col_index(ref.group(1)) + i) for i in range(len(captions))]
        spec = dict(title=title, sheet_part=sheet_part, table_part=table_part,
                    header_row=int(ref.group(2)), captions=captions, letters=letters)
        spec["rows"] = read_rows(parts[sheet_part].decode("utf-8"), spec)
        sheets.append(spec)
    return dict(names=names, parts=parts, sheets=sheets)


def read_rows(sheet_xml, spec):
    by_letter = dict(zip(spec["letters"], spec["captions"]))
    rows = []
    for _, number, body in ROW.findall(sheet_xml):
        if int(number) <= spec["header_row"]:
            continue
        values = {}
        for _, letter, inner in CELL.findall(body):
            found = TEXT.search(inner or "")
            values[by_letter.get(letter)] = unescape(found.group(1)).strip() if found else ""
        if any(values.values()):
            rows.append(values)
    return rows


def write_workbook(book, rows_by_title, path):
    """Rewrite each sheet's data rows (others emptied), keeping everything else."""
    parts = dict(book["parts"])
    for spec in book["sheets"]:
        rows = rows_by_title.get(spec["title"], [])
        sheet = parts[spec["sheet_part"]].decode("utf-8")
        prefix = re.search(r"<(\w+:)?worksheet\b", sheet).group(1) or ""
        head, rest = re.split(rf"<{prefix}sheetData\s*>", sheet, maxsplit=1)
        body, tail = re.split(rf"</{prefix}sheetData>", rest, maxsplit=1)
        header = "".join(m.group(0) for m in ROW.finditer(body) if int(m.group(2)) <= spec["header_row"])
        first = spec["header_row"] + 1
        out = []
        for offset, record in enumerate(rows):
            r = first + offset
            cells = "".join(
                f'<{prefix}c r="{letter}{r}" s="1" t="inlineStr"><{prefix}is>'
                f'<{prefix}t xml:space="preserve">{escape(fmt(record.get(caption)))}</{prefix}t>'
                f'</{prefix}is></{prefix}c>'
                for letter, caption in zip(spec["letters"], spec["captions"]))
            out.append(f'<{prefix}row r="{r}">{cells}</{prefix}row>')
        last_row = first + max(len(rows), 1) - 1
        last_col = spec["letters"][-1]
        head = re.sub(r'(<(?:\w+:)?dimension\b[^>]*\sref=")[^"]*(")',
                      rf"\g<1>A1:{last_col}{last_row}\g<2>", head)
        parts[spec["sheet_part"]] = (f"{head}<{prefix}sheetData>{header}{''.join(out)}"
                                     f"</{prefix}sheetData>{tail}").encode("utf-8")
        span = f"{spec['letters'][0]}{spec['header_row']}:{last_col}{last_row}"
        table = parts[spec["table_part"]].decode("utf-8")
        table = re.sub(r'(<(?:\w+:)?table\b[^>]*\sref=")[^"]*(")', rf"\g<1>{span}\g<2>", table, count=1)
        table = re.sub(r'(<(?:\w+:)?autoFilter\b[^>]*\sref=")[^"]*(")', rf"\g<1>{span}\g<2>", table, count=1)
        parts[spec["table_part"]] = table.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in book["names"]:
            z.writestr(name, parts[name])


def fmt(value):
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        out = format(value.normalize(), "f")
        return "0" if out in ("-0", "") else out
    return str(value)


# ---- Placing lines in BC ---------------------------------------------------------------


def place_lines(lines, existing, review):
    """Line numbers after BC's existing ones; drop lines BC already has."""
    highest, already = {}, set()
    for r in existing:
        key = (r.get("Project No.", ""), r.get("Project Task No.", ""))
        try:
            highest[key] = max(highest.get(key, 0), int(Decimal(r.get("Line No.", "0") or "0")))
        except ArithmeticError:
            pass
        already.add(key + (r.get("Line Type", "").upper(), r.get("No.", ""),
                           r.get("Description", "").upper()))

    records = []
    order = {"Budget": 0, "Billable": 1}
    for line in sorted(lines, key=lambda l: (l["project"], l["task"], l["item_no"],
                                             order.get(l["line_type"], 2))):
        key = (line["project"], line["task"])
        if key + (line["line_type"].upper(), line["gl"], line["description"].upper()) in already:
            review.append(dict(project=line["project"], code=line["code"],
                               description=line["description"],
                               issue="skipped: same line already in BC"))
            continue
        highest[key] = highest.get(key, 0) + LINE_NO_STEP
        records.append({
            "Project No.": line["project"], "Project Task No.": line["task"],
            "Line No.": highest[key], "Planning Date": line["date"],
            "Planned Delivery Date": line["date"], "Type": LINE_KIND, "No.": line["gl"],
            "Description": line["description"], "Quantity": line["quantity"],
            "Line Type": line["line_type"], "Unit Price": line["price"],
            "Qty. to Transfer to Invoice": line["quantity"] if line["line_type"] == "Billable" else None,
        })
    return records


def missing_tasks(records, setup_book, review):
    if setup_book is None:
        return []
    task_sheet = next(s for s in setup_book["sheets"] if "Task" in s["title"])
    have = {(r.get("Project No.", ""), r.get("Project Task No.", "")) for r in task_sheet["rows"]}
    needed = sorted({(r["Project No."], r["Project Task No."]) for r in records} - have)
    for project, task in needed:
        review.append(dict(project=project, issue="task to create (SOV_TASKS file)", detail=task))
    return [{"Project No.": p, "Project Task No.": t,
             "Description": SOV_TASK_DESCRIPTION if t == SOV_TASK else t,
             "Project Task Type": TASK_TYPE, "Project Posting Group": TASK_POSTING_GROUP}
            for p, t in needed]


def batches(records):
    if not BATCH_SIZE or len(records) <= BATCH_SIZE:
        return [records]
    out, current = [], []
    by_project = {}
    for r in records:
        by_project.setdefault(r["Project No."], []).append(r)
    for group in by_project.values():
        if current and len(current) + len(group) > BATCH_SIZE:
            out.append(current)
            current = []
        current.extend(group)
    if current:
        out.append(current)
    return out


# ---- Main ------------------------------------------------------------------------------


def report(items, records, tasks, review):
    print(f"\n   {items['customer_id'].nunique():>5} contract(s) with a priced SOV, "
          f"{len(items)} priced item(s)")
    df = pd.DataFrame(records)
    if not df.empty:
        df["amount"] = [q * p for q, p in zip(df["Quantity"], df["Unit Price"])]
        for line_type, group in df.groupby("Line Type"):
            print(f"   {len(group):>5} {line_type:<8} line(s)  {group['Project No.'].nunique():>4} project(s)"
                  f"  {sum(group['amount'], Decimal(0)):>18,.2f}")
    print(f"   {len(tasks):>5} task(s) to create first")

    issues = pd.DataFrame(review)
    if not issues.empty:
        print("\n   review file:")
        for issue, count in issues["issue"].value_counts().items():
            print(f"   {count:>5}  {issue}")
        unmapped = items[(items["ss4_gl"] == "") & ~items["code"].isin(GL_OVERRIDES)]
        if not unmapped.empty:
            totals = unmapped.groupby("code")["contract_amount"].agg(["count", "sum"])
            print("\n   codes with no G/L account in ss4 (add to GL_OVERRIDES if they should load):")
            for code, row in totals.sort_values("sum", ascending=False).iterrows():
                print(f"   {code or '(blank)':<8} {int(row['count']):>4} item(s)  {row['sum']:>16,.2f}")


def main():
    print(f"reading {SERVER} (company {COMPANY_CODE}, route {ROUTE!r}) ...")
    book = read_workbook(TEMPLATE_PATH)
    lines_sheet = book["sheets"][0]
    if "Line Type" not in lines_sheet["captions"] or "Unit Price" not in lines_sheet["captions"]:
        raise SystemExit("template needs Line Type and Unit Price columns")
    setup_book = read_workbook(SETUP_TEMPLATE_PATH) if SETUP_TEMPLATE_PATH else None
    print(f"   template: {len(lines_sheet['rows'])} existing planning line(s)")

    engine = create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(ODBC))
    with engine.connect() as conn:
        items = load_sov_items(conn)

    review = []
    for row in items[items["sov_count"] > 1].drop_duplicates("customer_id").itertuples(index=False):
        note(review, row, "contract has more than one SOV; latest used", f"{row.sov_count} SOVs")

    lines = build_lines(items, review)
    records = place_lines(lines, lines_sheet["rows"], review)
    tasks = missing_tasks(records, setup_book, review)
    report(items, records, tasks, review)

    print()
    parts = batches(records)
    for number, part in enumerate(parts, start=1):
        path = (OUTPUT_PATH if len(parts) == 1 else
                OUTPUT_PATH.with_name(f"{OUTPUT_PATH.stem}_part{number}{OUTPUT_PATH.suffix}"))
        write_workbook(book, {lines_sheet["title"]: part}, path)
        print(f"-> {path.name}   {len(part)} line(s)")
    if tasks:
        task_title = next(s["title"] for s in setup_book["sheets"] if "Task" in s["title"])
        write_workbook(setup_book, {task_title: tasks}, TASKS_OUTPUT_PATH)
        print(f"-> {TASKS_OUTPUT_PATH.name}   {len(tasks)} task(s): import through RENT-SETUP, "
              "apply table 1001 BEFORE the planning lines")
    pd.DataFrame(review).to_csv(REVIEW_PATH, index=False)
    print(f"-> {REVIEW_PATH.name}")


if __name__ == "__main__":
    main()