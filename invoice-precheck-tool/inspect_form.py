# -*- coding: utf-8 -*-
"""Inspect the FORM (customform) field options on a Sales Order — read only."""
import sys
from netsuite_scraper import PlaywrightSource, _clean

so_number = sys.argv[1] if len(sys.argv) > 1 else "SO9000007"
src = PlaywrightSource()
tid, root = src.resolve_so(so_number)
rec = root.find("record")
print("SO", so_number, "| internal id", tid)
print("  status      :", _clean(rec.findtext("status")))
print("  customform  :", _clean(rec.findtext("customform")))

# read the edit page's form dropdown (no changes, no save)
src._ensure()
page = src._ctx.new_page()
page.goto(f"https://1234567.app.netsuite.com/app/accounting/transactions/salesord.nl?id={tid}&e=T",
          wait_until="domcontentloaded", timeout=90000)
page.wait_for_timeout(4000)
info = page.evaluate("""() => {
  const sel = document.querySelector('#customform') ||
              document.querySelector('[name="customform"]');
  const cur = document.querySelector('#customform_fs_inpt');
  const out = {found: !!sel, current: cur ? cur.value : null, options: []};
  if (sel && sel.options) {
    out.options = [...sel.options].map(o => ({value: o.value, text: o.text}));
  }
  return out;
}""")
print("  dropdown found:", info["found"], "| current:", info["current"])
for o in info["options"]:
    print(f"    {o['value']:>10}  {o['text']}")
page.close()
src.close()
