# Billing app integration (XXX Order Workflow)

Internal LAN app: `http://192.168.0.1:3000/` ("XXX – Order
Workflow"). It is the daily source of SOs to invoice and the destination for
invoice back-fill. Only reachable on the office network.

## Reading the daily billing queue (read-only, no login needed)

`GET http://192.168.0.1:3000/db` returns the full database as JSON.

The Team 3 billing queue is:

```
orders where workflow_stage == "Team3" and (billing_status or "Unbilled") == "Unbilled"
```

Relevant order fields:

| Field | Meaning |
|---|---|
| `so_number` | NetSuite SO number (e.g. `SO9000013`) — feed into the pre-check |
| `account_number` | XXX customer code, **may carry the overseas suffix** (`5048 -CAN`, `5117 -Montreal`) |
| `po_number`, `order_type`, `total_qty`, `shipped_qty`, `work_status` | context for the report |
| `invoice_number`, `invoice_amount`, `billing_status`, `billed_at` | billing state (empty/Unbilled before back-fill) |

Queue-mode run = read the queue, deduplicate SO numbers, then run the normal
per-SO pre-check workflow on each. Cross-check `account_number` against the
customer resolved from NetSuite (Step 2); a mismatch is `NEEDS-REVIEW`.

If `GET /db` fails or returns unparseable JSON, report the batch as blocked —
do not silently fall back to an empty queue.

## Back-filling invoice results (write — strict rules)

After the user creates invoices in NetSuite, they provide an Excel containing
at least: SO number, Invoice number, Invoice amount (optionally invoice date).

**Write only through the app's own UI (browser automation), never by POSTing
`/db` directly.** `DB.save()` posts the entire orders array; a partial or stale
POST can clobber other users' concurrent edits. The UI path
(`markBilled`) applies the app's own validation, logging, timestamps, and sync
logic. The app requires a logged-in app user for edit actions — the user must
be logged into the app in the browser first.

Per SO, on the Team 3 – Billing tab:

1. Find the row for the SO. It must be `workflow_stage == Team3` and
   `billing_status == Unbilled` (the app rejects anything else — treat as a
   skip, report it).
2. Fill the row's inline `inv-num` and `inv-amt` inputs with the Excel values.
3. Click that row's `Billed` button.
4. Confirm the success notification / row state change before moving on.

Safety rules:

- **两条回填路径，确认要求不同**（Jing 2026-08-17）：
  - **队列扫描里的自动回填** —— 扫描发现「NetSuite 已开票、app 仍
    Unbilled」时自动写回。发票号和金额是从发票记录上读到的事实，不含任何
    判断，因此不需要逐批确认。
  - **用户拿 Excel 来的手工回填** —— 仍然要先给预览表（SO → INV# → 金额，
    外加对不上的行）并在对话里拿到明确确认**才能写第一行**；一次确认覆盖
    整批，但用户排除掉的行绝不写。
- Validate the Excel first: SO exists in the queue, invoice number non-empty,
  amount parses as a number. Rows failing validation are reported, not
  written.
- An SO already `Billed` with the **same** invoice number/amount → skip
  silently (idempotent re-run). Same SO with **different** values → do not
  overwrite; report the conflict for the user to resolve in the app's Edit
  Billing dialog.
- Excel rows with no matching queue row, and queue rows with no Excel row,
  both go in the summary so nothing disappears.
- 回填本身绝不碰 NetSuite。但**「NetSuite 全程只读」这条已经不成立了**：
  队列扫描会切换 SO 的 FORM 字段（211 → 219），这是 Jing 2026-08-17 明确
  授权的唯一一处 NetSuite 写入，细节见 SKILL.md「工具会写什么」。除该字段
  外仍然一个字段都不碰。
- 逐条保存会把服务器打挂：`DB.save()` 每次整包上传全部订单，逐行触发会让
  请求堆积挂起，而应用只在 `.then/.catch` 里累计失败数 —— 挂起的请求看起来
  和成功一模一样。正确做法是抑制逐条保存、最后只提交一次并等返回，再用
  独立的 `GET /db` 核对。**永远不要拿浏览器内存里的状态当写入成功的证据。**
