# -*- coding: utf-8 -*-
"""End-to-end live test: fetch one SO via CDP Chrome, run all checks."""
import sys
from netsuite_scraper import PlaywrightSource
import engine
import sources

so_num = sys.argv[1] if len(sys.argv) > 1 else "SO9000004"
src = PlaywrightSource()
so = src.fetch_so(so_num)
cust = src.fetch_customer(so["customer_internal_id"])
src.close()

wb = sources.load_reference_workbook()
row = wb["customers"].get(cust["internal_id"])
if row is None:
    print("customer absent from workbook -> DO-NOT-INVOICE")
    sys.exit(0)
cust["handling"] = row["handling"]
if row["can_rule"] == "FORCE-CAN":
    cust["overseas"] = True
elif row["can_rule"] == "NOT-CAN":
    cust["overseas"] = False

rep = engine.run_checks(so, cust, wb, wb["rules"])
print("OVERALL:", rep["overall"])
for k, v in rep["checks"].items():
    print(f"  {k}: {v['status']}")
    for e in v["evidence"]:
        print(f"      {e}")
print("customer:", cust["name"], "| internal id", cust["internal_id"],
      "| PL", cust["price_level"], f"(id {cust['price_level_id']})")
print("lines:", len(so["lines"]), "| fulfillments:", so["fulfillments"],
      "| invoices:", so["invoices"])
