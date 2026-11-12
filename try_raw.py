from connections import BcClient, BcTarget, ss4_engine
from stage import fetch_bc_raw, stage_bc_raw

target = BcTarget.from_env("BC")
print("fetching all fields of items from BC ...")
rows = fetch_bc_raw(BcClient(), target, "standard", "items")
print(f"   {len(rows):,} rows")

with ss4_engine().connect() as conn:
    table, fields = stage_bc_raw(conn, rows)
    print(f"staged into {table}: {len(fields)} fields")
    print("  ", ", ".join(fields))

    # The kind of question Claude will ask: does BC's 'number' match parts_bc.PartID?
    matched = conn.exec_driver_sql(f"""
        SELECT COUNT(*) FROM {table} b
        WHERE EXISTS (SELECT 1 FROM IMData.dbo.parts_bc s
                      WHERE s.CompanyCode = '01'
                        AND LTRIM(RTRIM(s.PartID)) COLLATE DATABASE_DEFAULT
                            = b.number COLLATE DATABASE_DEFAULT)""").scalar()
    print(f"\n{matched:,} of {len(rows):,} BC items match a parts_bc PartID (company 01)")