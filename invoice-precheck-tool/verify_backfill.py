# -*- coding: utf-8 -*-
"""Independent server-side verification of a backfill run."""
import json
import sys
import urllib.request

path = sys.argv[1] if len(sys.argv) > 1 else "reports/backfill_preview.json"
with open(path, encoding="utf-8") as f:
    rows = json.load(f)
with urllib.request.urlopen("http://192.168.0.1:3000/db", timeout=30) as r:
    db = json.load(r)

idx = {o["so_number"]: o for o in db["orders"]}
ok = bad = 0
total = 0.0
for x in rows:
    o = idx.get(x["so_number"], {})
    same_amt = str(o.get("invoice_amount", "")).replace(",", "") == str(x["invoice_amount"])
    if o.get("billing_status") == "Billed" and o.get("invoice_number") == x["invoice_number"] and same_amt:
        ok += 1
        total += float(x["invoice_amount"])
    else:
        bad += 1
        print("MISMATCH", x["so_number"], o.get("billing_status"), o.get("invoice_number"))

print("server-verified {}/{}, mismatches {}, total ${:,.2f}".format(ok, len(rows), bad, total))
q = [o for o in db["orders"]
     if o.get("workflow_stage") == "Team3" and o.get("billing_status", "Unbilled") == "Unbilled"]
print("Team3 unbilled queue now:", len(q))
