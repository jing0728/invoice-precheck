# Reference workbook schema — `invoice-pre-check-reference.xlsx`

One Project file, owned by Admin. The skill reads it and never edits it during
an SO check. Layout of every sheet: rows 1–4 are workbook-level controls
(label in column A, value in column B), row 6 is a legend, row 7 is the table
header, data starts at row 8.

## Workbook-level controls (every sheet, rows 1–4)

| Row | Label | Valid values |
|---|---|---|
| 1 | `Data Status` | `TEMPLATE` or `ACTIVE` |
| 2 | `Schema Version` | must equal Rules `SUPPORTED_SCHEMA_VERSION` |
| 3 | `Last Updated At` | date, `yyyy-mm-dd` |
| 4 | `Updated By` | free text |

All four sheets must be `ACTIVE` on a matching schema version before any SO is
checked. Surface `Last Updated At` / `Updated By` in the report so staleness is
visible.

## Fail-closed integrity rules

The following make the **affected checks fail closed** (the customer match
failing closed means `DO-NOT-INVOICE`; a discount sheet failing closed means
Check 1 cannot `PASS`):

- missing sheet, sheet still `TEMPLATE`, or schema version mismatch
- duplicate keys (same Customer Internal ID with overlapping effective periods;
  duplicate Rule ID + Customer Internal ID with overlapping periods; duplicate
  Record ID)
- invalid or unparseable dates; `Effective From` after `Effective To`
- malformed percentages (PERCENT values are fractions: `0.03` = 3%; a value
  ≥ 1 for PERCENT is malformed)
- ambiguous or contradictory rules that match the same SO

### 工具目前实际做到了哪几条（2026-08-18 核对）

已实现：

- 缺表 / 仍是 `TEMPLATE` / schema 版本不符 → 整个工作簿加载失败，当天全
  队列记 `CHECK-INCOMPLETE`，且**不产生任何写入**。
- **Customers 的生效期选行**：一个客户内部 ID 可以有多行（不同生效期），
  按 SO `DATE` 挑出适用的那一行。空的 `Effective From`/`Effective To` 表示
  该端不限。没有生效行、多行同时生效（生效期重叠）、生效日期无法解析、
  起止颠倒 —— 都让**这个客户**禁止开票，并在报告里说明是哪一种。
- 生效期问题**只影响该客户**，不影响当天其余的单。（这一点是刻意的：早先
  同一客户写两行会触发"重复内部 ID"、让整个工作簿加载失败，一个人加一行
  就能让当天的队列全部跑不了。）

尚未实现，走人工路径时要自己留神：

- Seasonal / Special 两张表的重复键与生效期重叠：不校验。
- PERCENT ≥ 1（把 3% 写成 `3`）：不校验。会当成 300% 折扣去算，结果因为
  金额对不上而 FAIL —— 拦得住，但报错信息完全指错方向。

## `Customers` sheet

| Column | Notes |
|---|---|
| `Customer Internal ID` | **primary matching key** — matched against the ID read from the customer record, never from the SO URL |
| `Customer Code` | evidence / cross-check only |
| `Customer Display Name` | evidence / cross-check only |
| `Handling Type` | `STANDARD` / `COD` / `COD-PR` — report flag only in v1 |
| `CAN Rule` | overseas override: `DEFAULT` (any dash-suffix on the name = overseas) / `FORCE-CAN` (overseas without suffix) / `NOT-CAN` (domestic despite suffix) |
| `Effective From`, `Effective To` | blank `Effective To` = open-ended |
| `Active` | `TRUE` / `FALSE` |
| `Notes` | free text |

Selection: the row where `Active = TRUE` and the effective period covers the SO
`DATE`. Every customer must be present, including `STANDARD` ones. No effective
row, multiple matching rows, or an ID/code/name inconsistency →
`DO-NOT-INVOICE`.

## `Seasonal Discounts` sheet

Columns: `Rule ID`, `Customer Internal ID`, `Reason`, `Discount Type`
(`PERCENT`/`AMOUNT`), `Discount Value`, `Effective From`, `Effective To`,
`Item Scope`, `Ordered Quantity Threshold`, `Cap Amount`, `Source / Notes`,
`Active`.

- Rules apply only to explicitly listed customers. A campaign covering several
  customers uses one row per customer and may share a `Rule ID`.
- `Item Scope` blank = the whole applicable shipped subtotal; otherwise an
  exact Item Code.
- `Ordered Quantity Threshold` means *that quantity or more*, judged on ordered
  quantity.
- Selection: `Active = TRUE`, Customer Internal ID matches, effective period
  covers the SO `DATE`.

## `Special Discounts` sheet

Columns: `Record ID`, `SO Number`, `PO Number`, `Customer Internal ID`,
`Reason`, `Discount Type`, `Discount Value`, `Item Scope`,
`Ordered Quantity Threshold`, `Cap Amount`, `Evidence Type`, `Evidence Date`,
`Evidence Summary`, `Entered By`, `Active`, `Notes`.

- Manually curated one-off discounts, matched by SO Number / PO Number /
  Customer Internal ID.
- Conflicting active records are **not** auto-resolved → the discount check
  reports `NEEDS-REVIEW`.

## `Rules` sheet

Key–value pairs (`Key`, `Value`, `Description`). v1 required keys:

| Key | v1 value |
|---|---|
| `MAX_ITEM_RATE_USD` | `1000` |
| `DISCOUNT_AMOUNT_TOLERANCE_USD` | `0` |
| `ROUNDING_MODE` | `HALF_UP` |
| `ROUNDING_SCALE` | `2` |
| `ALLOWED_SO_STATUSES` | `Partially Fulfilled\|Pending Billing` |
| `ALLOWED_RESULT_STATUSES` | `PASS\|FAIL\|NEEDS-REVIEW\|N/A` |
| `SUPPORTED_SCHEMA_VERSION` | `1.0` |

Multi-value lists use `|` as separator. Always read these values from the
sheet instead of hardcoding them; a missing required key blocks the run.
