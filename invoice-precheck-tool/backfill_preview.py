# -*- coding: utf-8 -*-
"""Collect invoice data from NetSuite for already-invoiced queue SOs (preview only)."""
import json
import re
import sys
from netsuite_scraper import PlaywrightSource, _clean
import sources

src = PlaywrightSource()
queue = sources.fetch_billing_queue()

preview = []
for order in queue:
    so_num = order["so_number"]
    try:
        so = src.fetch_so(so_num)
    except Exception as e:
        continue
    if not so["invoices"]:
        continue
    for inv in so["invoices"]:
        root = src._get_xml(f"/app/accounting/transactions/custinvc.nl?id={inv['id']}&xml=T")
        rec = root.find("record")
        total = _clean(rec.findtext("total")) if rec is not None else ""
        invdate = _clean(rec.findtext("trandate")) if rec is not None else inv.get("date", "")
        preview.append({"so_number": so_num, "account": order.get("account_number", ""),
                        "so_status": so["status"], "invoice_number": inv["number"],
                        "invoice_amount": total, "invoice_date": invdate,
                        "invoice_status": inv.get("status", "")})
src.close()

out = r"C:\Users\<user>\Desktop\invoice审查系统\invoice-precheck-tool\reports\backfill_preview.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(preview, f, ensure_ascii=False, indent=1)
for p in preview:
    print(f"{p['so_number']:12} {p['account']:16} {p['so_status']:8} -> "
          f"{p['invoice_number']:12} ${p['invoice_amount']:>12} ({p['invoice_date']}, {p['invoice_status']})")
print(f"\n{len(preview)} rows saved to backfill_preview.json")
