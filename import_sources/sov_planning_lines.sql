-- SOV items used to build the SOV planning lines import (RENT-PLANLINES, table 1003).
-- Taken from SQL_SOV_ITEMS in sov_planning_lines.py, with its parameters filled in:
--   company 01, rent code RNT, as of 2026-10-30, last rent invoice since 2026-09-01,
--   contract status A.
-- One row per SOV item on each current contract's latest SOV -- ALL items, including
-- zero-quantity and zero-rate lines (rate-card items). The script's original filter
-- (Units <> 0 AND NewContractAmount <> 0, priced items only) was not used in the
-- import that was actually loaded, so it is left out here.

WITH RentInvoices AS (
    SELECT i.[Type], i.InvoiceDate, LTRIM(RTRIM(i.CustomerID)) AS CustomerID,
           ROW_NUMBER() OVER (PARTITION BY LTRIM(RTRIM(i.CustomerID))
                              ORDER BY i.InvoiceDate DESC, i.InvoiceID DESC) AS rn
    FROM ARData.dbo.ARInvoice i WITH (NOLOCK)
    WHERE i.Company = '01' AND i.[Type] IN ('R', 'A')
      AND i.ProcessStatus = 'FINISHED'
      AND EXISTS (SELECT 1 FROM ARData.dbo.ARInvoiceDetail x WITH (NOLOCK)
                  WHERE x.InvoiceID = i.InvoiceID AND x.Item = 1
                    AND x.Quantity <> 0 AND x.Code = 'RNT')
),
Current_ AS (
    SELECT ri.CustomerID, ri.[Type] AS rent_type, c.ErectionDate, c.DismantleDate
    FROM RentInvoices ri
    JOIN EQData.dbo.Customer c WITH (NOLOCK) ON LTRIM(RTRIM(c.CustomerID)) = ri.CustomerID
    WHERE ri.rn = 1 AND ri.InvoiceDate >= '2026-09-01'
      AND c.DismantleDate > '2026-10-30' AND c.ContractStatus = 'A'
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
  ON b.Company = '01' AND b.Code = d.BillAsCode
ORDER BY cur.CustomerID, d.SOVItemNumber
