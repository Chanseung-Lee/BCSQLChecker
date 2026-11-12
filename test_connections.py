"""Checks that both connections work. Run: python test_connections.py"""

from sqlalchemy import text

from connections import BcClient, BcTarget, ss4_engine


def test_ss4():
    print("SS4 ...")
    with ss4_engine().connect() as conn:
        row = conn.execute(text("""
            SELECT @@SERVERNAME AS server_name, DB_NAME() AS db, SUSER_SNAME() AS login_name,
                   IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin,
                   IS_ROLEMEMBER('db_owner') AS is_db_owner,
                   IS_ROLEMEMBER('db_datawriter') AS is_writer
        """)).mappings().one()
        print(f"   connected to {row['server_name']}/{row['db']} as {row['login_name']}")
        if row["is_sysadmin"] or row["is_db_owner"] or row["is_writer"]:
            print("   note: this login can WRITE. Fine for development; "
                  "give the agent a read-only login before it explores SS4.")
        count = conn.execute(text("SELECT COUNT(*) FROM EQData.dbo.Customer")).scalar()
        print(f"   cross-database read OK: EQData.dbo.Customer has {count:,} rows")


def test_bc():
    print("BC ...")
    target = BcTarget.from_env("BC")
    client = BcClient()
    companies = client.companies(target)
    print(f"   token OK; {len(companies)} company(ies) visible in {target.environment}:")
    for c in companies:
        mark = "  <- configured" if c["id"].lower() == target.company_id.lower() else ""
        print(f"     {c['name']:<30} {c['id']}{mark}")
    if not any(c["id"].lower() == target.company_id.lower() for c in companies):
        print("   WARNING: BC_COMPANY_ID is not among them. Check the id, or that the "
              "app has been granted access to that company in BC.")


if __name__ == "__main__":
    for test in (test_ss4, test_bc):
        try:
            test()
        except Exception as ex:
            print(f"   FAILED: {ex}")
        print()
