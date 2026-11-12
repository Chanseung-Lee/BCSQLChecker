from sqlalchemy import text

from connections import BcClient, ss4_engine
from spec import load_spec
from stage import fetch_bc, stage_bc

spec = load_spec("specs/partsUS.yaml")
print(f"fetching {spec.bc.entity} from BC ...")
rows = fetch_bc(spec, BcClient())
print(f"   {len(rows):,} rows")

with ss4_engine().connect() as conn:
    table = stage_bc(conn, spec, rows)
    count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
    print(f"staged into {table}: {count:,} rows")
    for r in conn.execute(text(f"SELECT TOP 5 * FROM {table} ORDER BY 1")).mappings():
        print("  ", dict(r))