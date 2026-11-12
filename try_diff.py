from collections import Counter, defaultdict

from connections import BcClient, ss4_engine
from diff import run_diff
from spec import load_spec
from stage import fetch_bc, stage_bc

spec = load_spec("specs/partsUS.yaml")
print(f"fetching {spec.bc.entity} from BC ...")
rows = fetch_bc(spec, BcClient())
print(f"   {len(rows):,} rows")

with ss4_engine().connect() as conn:
    stage_bc(conn, spec, rows)
    findings = run_diff(conn, spec)

print(f"\n{len(findings):,} finding(s)")
for (kind, severity), n in Counter((f.kind, f.severity) for f in findings).most_common():
    print(f"   {n:>7,}  {kind:<15} [{severity}]")

by_column = Counter(f.column for f in findings if f.kind == "mismatch")
if by_column:
    print("\nmismatches by column:")
    for column, n in by_column.most_common():
        print(f"   {n:>7,}  {column}")

examples = defaultdict(list)
for f in findings:
    if len(examples[(f.kind, f.column)]) < 3:
        examples[(f.kind, f.column)].append(f)

print("\nexamples:")
for (kind, column), items in examples.items():
    print(f"   {kind}{' / ' + column if column else ''}:")
    for f in items:
        if kind == "mismatch":
            values = f"   BC={f.bc_value!r}  SS4={f.ss4_value!r}"
        elif kind.startswith("duplicate"):
            values = f"   {f.ss4_value or f.bc_value}"
        else:
            values = ""
        print(f"      {f.key}{values}")