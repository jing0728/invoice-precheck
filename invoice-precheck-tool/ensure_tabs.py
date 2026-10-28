# -*- coding: utf-8 -*-
"""Make sure the debug Chrome has the tabs the tool needs, after a cold start.

Opens NetSuite and the billing app if they are not already open. Cannot log in
(credentials are the user's job) — the tool's preflight reports what is missing.
"""
import sys
import time

NETSUITE = "https://1234567.app.netsuite.com/app/center/card.nl"
BILLING = "http://192.168.0.1:3000"


def main():
    from playwright.sync_api import sync_playwright
    for attempt in range(10):          # Chrome may still be starting up
        try:
            pw = sync_playwright().start()
            b = pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
            break
        except Exception:
            time.sleep(3)
    else:
        print("CDP not reachable — is the debug Chrome running?")
        return 1

    ctx = b.contexts[0] if b.contexts else b.new_context()
    urls = [p.url for p in ctx.pages]
    for target, name in ((NETSUITE, "NetSuite"), (BILLING, "billing app")):
        origin = target.split("/app/")[0] if "netsuite" in target else target
        if any(u.startswith(origin) for u in urls):
            print(f"{name}: tab already open")
            continue
        p = ctx.new_page()
        try:
            p.goto(target, wait_until="domcontentloaded", timeout=60000)
            print(f"{name}: tab opened")
        except Exception as e:
            print(f"{name}: open failed — {type(e).__name__}")
    pw.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
