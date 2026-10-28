# -*- coding: utf-8 -*-
"""Diagnose the billing app tab's sync state."""
from playwright.sync_api import sync_playwright

JS = """() => ({
  url: location.href,
  serverMode: window._serverMode,
  saveFailures: window._saveFailures || 0,
  pending: !!window._pendingSavePayload,
  user: (typeof Auth !== 'undefined' && Auth.currentUser) ? Auth.currentUser.fullName : null,
  orders: (typeof DB !== 'undefined') ? DB.orders.length : null,
  sample: (typeof DB !== 'undefined') ? (() => {
     const o = DB.getOrder('SO9000004');
     return o ? [o.billing_status, o.invoice_number, o.updated_at] : null; })() : null,
  billedCount: (typeof DB !== 'undefined')
     ? DB.orders.filter(o => o.billing_status === 'Billed').length : null
})"""

pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
for ctx in b.contexts:
    for p in ctx.pages:
        if p.url.startswith("http://192.168.0.1:3000"):
            try:
                print(p.evaluate(JS))
            except Exception as e:
                print("eval failed:", e)
pw.stop()
