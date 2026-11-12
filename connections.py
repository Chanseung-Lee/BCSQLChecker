"""
Connections to SS4 (SQL Server) and Business Central, configured from .env.

SS4: same pattern as the existing import scripts (SQLAlchemy over pyodbc).
     Windows authentication by default; set SS4_USER / SS4_PASSWORD to use a
     SQL login instead (the agent should use a dedicated READ-ONLY login --
     read-only is enforced by that login's permissions, not by this code).

BC:  same pattern as MorrowAPI: an Entra app with client credentials, one
     cached token, and the three URL shapes (API v2.0, ODataV4 pages, and
     AlphaBOLD's custom API).
"""

import os
import time
import urllib.parse
from dataclasses import dataclass

import httpx
import msal
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

import json
from decimal import Decimal

load_dotenv()

BC_HOST = "https://api.businesscentral.dynamics.com"
BC_SCOPE = [f"{BC_HOST}/.default"]
PAGE_SIZE = 1000


# ---------------------------------------------------------------- SS4

def ss4_engine(database: str = "SAPData") -> Engine:
    """SQLAlchemy engine for one SS4 database. Other databases on the server
    are reachable by three-part name (EQData.dbo.Customer), as in the scripts."""
    driver = os.getenv("SS4_ODBC_DRIVER", "ODBC Driver 17 for SQL Server")
    server = os.getenv("SS4_SERVER", "ss4")
    user, password = os.getenv("SS4_USER"), os.getenv("SS4_PASSWORD")
    auth = f"UID={user};PWD={password};" if user else "Trusted_Connection=yes;"
    odbc = (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
            f"{auth}TrustServerCertificate=yes;")
    return create_engine("mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(odbc),
                         fast_executemany=True)


# ---------------------------------------------------------------- BC targets

@dataclass(frozen=True)
class BcTarget:
    """Which BC environment and company to read. Mirrors MorrowAPI's BcTarget."""
    tenant_id: str
    environment: str
    company: str        # BC's internal company NAME, used by ODataV4 pages
    company_id: str     # company GUID, used by the APIs

    @property
    def env_base(self) -> str:
        return f"{BC_HOST}/v2.0/{self.tenant_id}/{self.environment}"

    @property
    def api_base(self) -> str:
        """Standard API v2.0, e.g. .../companies({id})/customers"""
        return f"{self.env_base}/api/v2.0/companies({self.company_id})"

    @property
    def odata_base(self) -> str:
        """Pages published as web services, e.g. .../ItemLedgerEntries.
        Addressed by company NAME, not id."""
        return f"{self.env_base}/ODataV4/Company('{urllib.parse.quote(self.company, safe='')}')"

    def custom_api_base(self, path_segment: str) -> str:
        """Custom API pages, e.g. path_segment='alphabold/morrow/v2.0'."""
        return f"{self.env_base}/api/{path_segment}/companies({self.company_id})"

    @classmethod
    def from_env(cls, prefix: str = "BC") -> "BcTarget":
        """Reads {prefix}_ENVIRONMENT etc., so several targets can live in one
        .env (e.g. BC_* for Production, BCUAT_* for UAT). Tenant is shared."""
        def need(name: str) -> str:
            value = os.getenv(f"{prefix}_{name}")
            if not value:
                raise RuntimeError(f"{prefix}_{name} is not set in .env")
            return value
        return cls(tenant_id=os.getenv("BC_TENANT_ID") or need("TENANT_ID"),
                   environment=need("ENVIRONMENT"),
                   company=need("COMPANY"),
                   company_id=need("COMPANY_ID"))


# ---------------------------------------------------------------- BC client

class BcClient:
    """Authenticated GETs against BC. Read-only by design: there is no
    method here that writes."""

    def __init__(self):
        tenant = os.getenv("BC_TENANT_ID")
        client_id, secret = os.getenv("BC_CLIENT_ID"), os.getenv("BC_CLIENT_SECRET")
        if not (tenant and client_id and secret):
            raise RuntimeError("BC_TENANT_ID, BC_CLIENT_ID and BC_CLIENT_SECRET must be set in .env")
        # msal caches the token and refreshes it when it nears expiry.
        self._app = msal.ConfidentialClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant}",
            client_credential=secret)
        self._http = httpx.Client(timeout=120)

    def _token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=BC_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(f"BC token request failed: {result.get('error_description', result)}")
        return result["access_token"]

    def get(self, url: str, params: dict | None = None) -> dict:
        """One GET, retrying on throttling (429) and BC being briefly unavailable."""
        for attempt in range(5):
            response = self._http.get(url, params=params, headers={
                "Authorization": f"Bearer {self._token()}",
                "Prefer": f"odata.maxpagesize={PAGE_SIZE}",
            })
            if response.status_code in (429, 503, 504):
                time.sleep(float(response.headers.get("Retry-After", 2 ** attempt)))
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"BC {response.status_code} for {url}: {response.text[:500]}")
            return json.loads(response.text, parse_float=Decimal)
        raise RuntimeError(f"BC kept throttling {url}; gave up after 5 attempts")

    def fetch_all(self, url: str, filter: str | None = None, select: str | None = None) -> list[dict]:
        """Every row of an entity, following @odata.nextLink.

        Never uses $top: BC treats it as a hard limit and sends no nextLink, so
        paging silently stops (found the hard way in MorrowAPI). Page size is
        requested with the Prefer header instead."""
        params = {k: v for k, v in {"$filter": filter, "$select": select}.items() if v}
        rows, next_url = [], url
        while next_url:
            page = self.get(next_url, params=params)
            rows.extend(page.get("value", []))
            next_url, params = page.get("@odata.nextLink"), None  # nextLink carries the query
        return rows

    def companies(self, target: BcTarget) -> list[dict]:
        """Companies this app can see in the target's environment."""
        return self.fetch_all(f"{target.env_base}/api/v2.0/companies")
