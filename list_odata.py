"""
Lists the pages published as OData web services in a BC environment.

    python list_odata.py            (the BC_* target in .env)
    python list_odata.py BCPROD     (another target, read from BCPROD_* in .env)

A page has to be published (in BC: search for "Web Services", add the page,
tick Published) before this tool can read it with api: odata. The names listed
here are what goes in a spec's bc.entity, or after `python mapper.py`.
"""

import sys

from connections import BcClient, BcTarget


def main() -> None:
    prefix = sys.argv[1] if len(sys.argv) > 1 else "BC"
    target = BcTarget.from_env(prefix)

    # The OData service root lists every published entity set in the environment.
    service = BcClient().get(f"{target.env_base}/ODataV4/")
    names = []
    for entry in service.get("value", []):
        if entry.get("kind", "EntitySet") == "EntitySet" and entry.get("name") != "Company":
            names.append(entry["name"])

    print(f"OData pages published in {target.environment} ({len(names)}):")
    for name in sorted(names, key=str.lower):
        print(f"   {name}")
    print(f"\nRead them in company '{target.company}' with: python mapper.py <name> --api odata"
          + (f" --target {prefix}" if prefix != "BC" else ""))


if __name__ == "__main__":
    main()
