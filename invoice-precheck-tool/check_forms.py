# -*- coding: utf-8 -*-
"""Read the current FORM (customform) of several SOs. Read only — never saves."""
import sys
import urllib.request
import json
from netsuite_scraper import PlaywrightSource

TARGET_FORM = "219"  # XXX - Sales Order with Rate with Close


def read_form(src, page, tid):
    page.goto(f"https://1234567.app.netsuite.com/app/accounting/transactions/salesord.nl?id={tid}&e=T",
              wait_until="domcontentloaded", timeout=90000)
    page.wait_for_function(
        "() => typeof nlapiGetFieldValue === 'function' && nlapiGetFieldValue('customform')",
        timeout=60000)
    return page.evaluate("""() => ({
        id: nlapiGetFieldValue('customform'),
        text: nlapiGetFieldText('customform'),
        status: nlapiGetFieldValue('orderstatus') || nlapiGetFieldText('orderstatus') || ''
    })""")


if __name__ == "__main__":
    sos = sys.argv[1:]
    if not sos:
        with urllib.request.urlopen("http://192.168.0.1:3000/db", timeout=30) as r:
            db = json.load(r)
        sos = [o["so_number"] for o in db["orders"]
               if o.get("workflow_stage") in ("Team2", "Team3")][:6]
        print("sampling from billing app:", sos)
    src = PlaywrightSource()
    src._ensure()
    page = src._ctx.new_page()
    for n in sos:
        try:
            tid, _ = src.resolve_so(n)
            f = read_form(src, page, tid)
            mark = "OK(219)" if f["id"] == TARGET_FORM else "NEEDS CHANGE"
            print(f"{n:12} id={tid:10} form={f['id']:>5} {f['text'][:45]:45} {mark}")
        except Exception as e:
            print(f"{n:12} error: {type(e).__name__}: {str(e)[:80]}")
    page.close()
    src.close()
