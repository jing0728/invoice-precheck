# -*- coding: utf-8 -*-
"""Reload the billing app tab so it re-syncs from the server, then report state."""
from playwright.sync_api import sync_playwright

pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
done = False
for ctx in b.contexts:
    for p in ctx.pages:
        if p.url.startswith("http://192.168.0.1:3000"):
            p.reload(wait_until="domcontentloaded", timeout=60000)
            p.wait_for_function(
                "() => typeof DB !== 'undefined' && Array.isArray(DB.orders) && DB.orders.length > 0",
                timeout=60000)
            p.wait_for_timeout(4000)  # let the initial server sync settle
            print(p.evaluate("""() => ({
                user: (typeof Auth !== 'undefined' && Auth.currentUser)
                        ? Auth.currentUser.fullName : null,
                orders: DB.orders.length,
                unbilledTeam3: DB.orders.filter(o => o.workflow_stage === 'Team3'
                        && (o.billing_status || 'Unbilled') === 'Unbilled').length,
                sample: (() => { const o = DB.getOrder('SO9000004');
                        return o ? [o.billing_status, o.invoice_number] : null; })()
            })"""))
            done = True
pw.stop()
if not done:
    print("no billing tab found")
