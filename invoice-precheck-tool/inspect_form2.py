# -*- coding: utf-8 -*-
"""Deeper probe of the FORM (customform) widget on the SO edit page — read only."""
import sys
from netsuite_scraper import PlaywrightSource

so_number = sys.argv[1] if len(sys.argv) > 1 else "SO9000007"
src = PlaywrightSource()
tid, _ = src.resolve_so(so_number)
src._ensure()
page = src._ctx.new_page()
page.goto(f"https://1234567.app.netsuite.com/app/accounting/transactions/salesord.nl?id={tid}&e=T",
          wait_until="domcontentloaded", timeout=90000)
page.wait_for_timeout(6000)

info = page.evaluate("""() => {
  const res = {elements: [], api: {}, options: []};
  document.querySelectorAll('[id*="customform"], [name*="customform"]').forEach(el => {
    res.elements.push({id: el.id, name: el.name, tag: el.tagName,
                       type: el.type || null,
                       value: (el.value !== undefined ? String(el.value).slice(0, 60) : null)});
  });
  try { res.api.nlapiGet = (typeof nlapiGetFieldValue === 'function')
        ? nlapiGetFieldValue('customform') : 'no nlapi'; } catch (e) { res.api.nlapiGet = 'err ' + e.message; }
  try { res.api.nlapiText = (typeof nlapiGetFieldText === 'function')
        ? nlapiGetFieldText('customform') : 'no nlapi'; } catch (e) { res.api.nlapiText = 'err ' + e.message; }
  // NetSuite keeps dropdown data in a JS object registered by field name
  try {
    const dd = window.getDropdown ? getDropdown(document.getElementById('customform')) : null;
    if (dd && dd.valueArray) {
      res.options = dd.valueArray.map((v, i) => ({value: v, text: dd.textArray[i]}));
      res.api.ddIndex = dd.currentIndex;
      res.api.ddValue = dd.getValue ? dd.getValue() : null;
      res.api.ddText = dd.getText ? dd.getText() : null;
    } else { res.api.dd = 'no dropdown object'; }
  } catch (e) { res.api.ddErr = String(e).slice(0, 120); }
  return res;
}""")

for e in info["elements"]:
    print("element:", e)
print("api:", info["api"])
print("options:")
for o in info["options"]:
    print(f"   {o['value']:>10}  {o['text']}")
page.close()
src.close()
