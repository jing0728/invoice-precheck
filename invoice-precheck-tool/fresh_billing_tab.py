# -*- coding: utf-8 -*-
"""Close the stuck billing tab, open a fresh one, report login/sync state."""
from playwright.sync_api import sync_playwright

APP = "http://192.168.0.1:3000"
pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
ctx = b.contexts[0]

for p in list(ctx.pages):
    if p.url.startswith(APP):
        try:
            p.close()
            print("closed stuck tab")
        except Exception as e:
            print("could not close tab:", e)

p = ctx.new_page()
p.goto(APP, wait_until="domcontentloaded", timeout=60000)
try:
    p.wait_for_function(
        "() => typeof DB !== 'undefined' && Array.isArray(DB.orders) && DB.orders.length > 0",
        timeout=60000)
except Exception as e:
    print("DB not ready:", type(e).__name__)
p.wait_for_timeout(3000)
print(p.evaluate("""() => ({
    user: (typeof Auth !== 'undefined' && Auth.currentUser) ? Auth.currentUser.fullName : null,
    orders: (typeof DB !== 'undefined') ? DB.orders.length : null,
    unbilledTeam3: (typeof DB !== 'undefined') ? DB.orders.filter(o =>
        o.workflow_stage === 'Team3' && (o.billing_status || 'Unbilled') === 'Unbilled').length : null,
    sample: (typeof DB !== 'undefined') ? (() => { const o = DB.getOrder('SO9000004');
        return o ? [o.billing_status, o.invoice_number] : null; })() : null
})"""))
pw.stop()
