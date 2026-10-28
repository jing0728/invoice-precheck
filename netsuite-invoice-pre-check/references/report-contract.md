# Report contract

> 这个文本模板用于**人工降级路径**。走工具时不需要套它 —— 工具直接产出
> JSON（`reports/*.json`）和网页，字段名是英文、显示层才翻成中文。

Return a batch summary followed by one report per SO, in the user's input
order (after deduplication). Do not write results back to Project files.
Separate observed facts, calculated values, and unresolved evidence. Report the
minimum evidence needed for a human to reproduce each result — never full email
bodies.

Per-check statuses: `PASS` | `FAIL` | `NEEDS-REVIEW` | `N/A`.
Overall statuses: `INVOICE-READY` | `DO-NOT-INVOICE` | `NEEDS-REVIEW` |
`CHECK-INCOMPLETE`.

## Template

```text
Batch summary
- Requested: <n> | Unique: <n> | Invoice-ready: <n> | Do-not-invoice: <n> | Needs-review: <n> | Incomplete: <n>
- Reference workbook: Last Updated At <date> by <name>; Schema Version <ver>

SO: <number>
Checked at: <timestamp and timezone>
Customer: <code and name> | Internal ID: <id>
Customer handling: STANDARD | COD | COD-PR
SO date/status: <date> | <status>
Overall: INVOICE-READY | DO-NOT-INVOICE | NEEDS-REVIEW | CHECK-INCOMPLETE

0. Eligibility: <status>
   Evidence: SO status; related Invoices; fulfillment list
1. Discount: <status>
   Sources: Account Notes / Memo / Excel / email state
   Calculation: ordered threshold, shipped basis, fixed amounts, compound rates, cap, expected vs actual
2. Price Level: <status>
   Customer default; distinct SO values; affected lines
3. Unit Price: <status>
   Exceptions with Line ID / Item / Price Level / Rate
4. Fulfillment: <status>
   All fulfillment numbers and statuses; overseas multi-load warning
5. Shipping Address / EDI: <status>
   Normalized match result or N/A

Required human actions:
- <specific actions, or None>
```

## Content rules

- **Checked at** — always include timezone; results are point-in-time. Remind
  the user to re-run immediately before invoicing if the SO may have changed.
- **Eligibility evidence** — list every fulfillment and invoice found with
  number, date, and status, plus the exact SO status observed.
- **Discount calculation** — show enough numbers for independent reproduction:
  each accepted discount with source, value, scope; the ordered-quantity
  threshold decision; the shipped basis amount; fixed subtraction; compounded
  rate; cap application; final expected amount vs the observed Summary
  `DISCOUNT ITEM` amount.
- **Required human actions** — concrete and specific ("Verify the Memo-only 5%
  discount against customer email, then re-run"), or `None`.
- A `CHECK-INCOMPLETE` SO still gets a report block: state what could not be
  read or proven and which checks did not run.
- Never let a `FAIL` or missing evidence disappear into `N/A`.
