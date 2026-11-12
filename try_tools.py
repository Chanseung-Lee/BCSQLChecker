"""Exercises the mapping agent's tools directly, without Claude, so you can see
exactly what Claude will see. Run: python try_tools.py"""

import asyncio
from pathlib import Path

from agent_tools import MappingSession, build_tools, profile_text
from connections import BcClient, BcTarget, ss4_engine
from stage import fetch_bc_raw, profile_bc, stage_bc_raw


def text(result):
    return result["content"][0]["text"]


async def main():
    print("fetching all fields of items from BC ...")
    rows = fetch_bc_raw(BcClient(), BcTarget.from_env("BC"), "standard", "items")

    with ss4_engine().connect() as conn:
        _, fields = stage_bc_raw(conn, rows)
        # Commit so #bc_raw survives rollbacks. A temp table created inside a
        # transaction that later rolls back is dropped along with it -- and
        # run_sql rolls back after every SQL error.
        conn.commit()

        print("\n--- BC field profile (goes into Claude's instructions)")
        print(profile_text(profile_bc(rows, fields)))

        session = MappingSession(conn, rows, target="BC", api="standard", entity="items")
        run_sql, check_spec, submit_spec = build_tools(session)

        print("\n--- a normal query")
        print(text(await run_sql.handler({"sql": "SELECT TOP 3 number, displayName, unitPrice FROM #bc_raw"})))

        print("\n--- a forbidden query")
        print(text(await run_sql.handler({"sql": "DELETE FROM #bc_raw"})))

        print("\n--- a broken query (the error comes back as text)")
        print(text(await run_sql.handler({"sql": "SELECT nosuchcolumn FROM #bc_raw"})))

        print("\n--- #bc_raw survived the rollback")
        print(text(await run_sql.handler({"sql": "SELECT COUNT(*) AS rows_in_bc_raw FROM #bc_raw"})))

        print("\n--- check_spec with our hand-written spec")
        spec_text = Path("specs/partsUS.yaml").read_text(encoding="utf-8")
        print(text(await check_spec.handler({"spec_yaml": spec_text})))


asyncio.run(main())
