# -*- coding: utf-8 -*-
"""Isolate whether the billing app's save actually reaches the server."""
from playwright.sync_api import sync_playwright

JS = """async () => {
  const before = DB.getOrder('SO9000004');
  // push current state exactly the way the app does
  const resp = await fetch('/db', {
    method: 'POST',
    headers: {'Content-Type': 'application/json',
              'X-Client-ID': String(window._clientId || 0)},
    body: JSON.stringify({orders: DB.orders, shipments: DB.shipments})
  });
  const bodyText = (await resp.text()).slice(0, 300);
  await new Promise(r => setTimeout(r, 2000));
  const server = await (await fetch('/db', {cache: 'no-store'})).json();
  const so = server.orders.find(o => o.so_number === 'SO9000004');
  return {
    localBefore: [before.billing_status, before.invoice_number, before.updated_at],
    postStatus: resp.status, postOk: resp.ok, postBody: bodyText,
    serverAfter: so ? [so.billing_status, so.invoice_number, so.updated_at] : null,
    serverOrderCount: server.orders.length, localOrderCount: DB.orders.length
  };
}"""

pw = sync_playwright().start()
b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
for ctx in b.contexts:
    for p in ctx.pages:
        if p.url.startswith("http://192.168.0.1:3000"):
            for k, v in p.evaluate(JS).items():
                print(f"{k}: {v}")
pw.stop()
