# -*- coding: utf-8 -*-
"""NetSuite data source via Playwright (persistent Chrome profile, read-only).

Strategy: NO DOM scraping. Every record is fetched as structured XML using the
classic UI's `&xml=T` parameter through an authenticated session:

  - SO:        /app/accounting/transactions/salesord.nl?id=<tid>&xml=T
  - Customer:  /app/common/entity/custjob.nl?id=<cid>&xml=T
  - SO-number -> transaction id: global search results page (href regex),
    then PROVEN by tranid equality on the fetched XML (plan: no fuzzy match).

Field-name map (account-specific, verified 2026-08-17 against XXX production):
  SO custbody36  = EDI INFO (contains "ShippingDetails:" block)
  SO custbody4   = Account Notes as displayed on the SO
  SO custbody_items_total_weight = whole-order material weight (lbs)
  IF machine[package] = pallet/package rows (packageweight each);
     IF onsaveshipmentweight = shipped weight, baseweightunit = 'lbs'
  Customer custentity4 = Account Notes (authoritative, customer record)
  Customer pricelevel  = default Price Level INTERNAL ID
  Item line: price = Price Level internal id, price_display = its name,
             quantityfulfilled = shipped-so-far qty, itemtype InvtPart = merchandise

Login: `python netsuite_scraper.py --login` opens a visible window; log into
NetSuite once (incl. MFA). Cookies persist in .browser-profile for later
headless runs until NetSuite expires the session.
"""
import html
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = "https://1234567.app.netsuite.com"
PROFILE = Path(__file__).resolve().parent / ".browser-profile"

F_EDI_INFO = "custbody36"
F_SO_NOTES = "custbody4"
F_CUST_NOTES = "custentity4"
F_TOTAL_WEIGHT = "custbody_items_total_weight"  # whole-order material weight, lbs


class NeedLoginError(RuntimeError):
    """NetSuite session missing/expired. Run: python netsuite_scraper.py --login"""


def _clean(s):
    """Decode entities and convert <br> to newlines in an XML text value."""
    if s is None:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    return s.strip()


def _edi_address_lines(raw):
    """Pull the shipping-address block out of the EDI INFO field.

    XXX writes it in two shapes, both asterisk-padded:
      "... prose ... ShippingDetails: \\n****100 SAMPLE CT \\n**SAMPLETOWN..."
      "****10 Sample Road \\n**Sampleville  ON  A1A 1A1\\n**************"
    The '*' prefix is what actually marks the block, so the marker word is
    optional. Returns None when the field carries no address block at all —
    an EDI field full of prose must not be compared as if it were an address.
    """
    if not raw:
        return None
    body = raw.split("ShippingDetails", 1)[1] if "ShippingDetails" in raw else raw
    lines = [l for l in body.lstrip(":").splitlines() if l.strip()]
    starred = [l for l in lines if l.lstrip().startswith("*")]
    if starred:
        return starred
    # marker present but no asterisk padding: trust the marker
    return lines if "ShippingDetails" in raw and lines else None


CDP_URL = "http://127.0.0.1:9222"  # same debug Chrome the other XXX tools use


class PlaywrightSource:
    """Connects to the user's running debug Chrome (CDP :9222) and reuses its
    logged-in NetSuite session — same convention as the replenishment and
    photo tools. Falls back to an own persistent profile if CDP is down."""

    def __init__(self, headless=True):
        self._headless = headless
        self._pw = self._ctx = self._page = None
        self._own_browser = False

    def _ensure(self):
        if self._page:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            browser = self._pw.chromium.connect_over_cdp(CDP_URL)
            self._ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            self._own_browser = False
        except Exception:
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(PROFILE), channel="chrome", headless=self._headless)
            self._own_browser = True
        # one worker tab on the NetSuite origin; all record fetches go through
        # same-origin fetch() so no further navigation/flicker happens
        self._page = self._ctx.new_page()
        self._page.goto(BASE + "/app/center/card.nl", wait_until="domcontentloaded",
                        timeout=45000)

    def close(self):
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
        if self._own_browser and self._ctx:
            self._ctx.close()
        if self._pw:
            self._pw.stop()
        self._pw = self._ctx = self._page = None

    def _get_text(self, path):
        self._ensure()
        text = self._page.evaluate(
            "url => fetch(url, {credentials: 'include'}).then(r => r.text())",
            BASE + path)
        if "NLPleaseLogin" in text or 'name="password"' in text:
            raise NeedLoginError()
        return text

    def _get_xml(self, path):
        text = self._get_text(path)
        m = re.search(r"<nsResponse[\s\S]*</nsResponse>", text)
        if not m:
            raise NeedLoginError()  # got HTML (login or error page) instead of record XML
        return ET.fromstring(m.group(0))

    # ── SO-number resolution (exact-match proven) ──────────────────────────
    def resolve_so(self, so_number):
        page = self._get_text(
            "/app/common/search/ubersearchresults.nl?quicksearch=T&searchtype=Uber"
            f"&Uber_NAMEtype=KEYWORDSTARTSWITH&Uber_NAME={so_number}")
        ids = list(dict.fromkeys(re.findall(r"transaction\.nl\?id=(\d+)", page)))
        for tid in ids[:5]:
            root = self._get_xml(f"/app/accounting/transactions/salesord.nl?id={tid}&xml=T")
            rec = root.find("record")
            if rec is not None and rec.get("recordType") == "salesorder" \
                    and _clean(rec.findtext("tranid")) == so_number:
                return tid, root
        raise LookupError(f"在 NetSuite 中找不到 SO {so_number}（搜索到的候选：{ids}）")

    # ── Normalized fetchers (shapes match engine.run_checks) ───────────────
    def fetch_so(self, so_number):
        tid, root = self.resolve_so(so_number)
        rec = root.find("record")
        g = lambda n: _clean(rec.findtext(n))

        # Merchandise item types. Restricting this to InvtPart made the Price
        # Level and Rate checks skip kits/assemblies entirely — an SO built
        # from kits passed the rate check vacuously ("0 rows all <= 1000").
        MERCH_TYPES = {"InvtPart", "Assembly", "Kit", "NonInvtPart", "OthCharge"}
        lines = []
        for ln in rec.iterfind('machine[@name="item"]/line'):
            lg = lambda n: _clean(ln.findtext(n))
            if lg("itemtype") not in MERCH_TYPES or not lg("item_display"):
                continue  # skip description/discount/subtotal rows
            lines.append({"line_id": lg("line"), "item": lg("item_display"),
                          "qty": lg("quantity"),
                          "price_level": lg("price_display"),
                          "price_level_id": lg("price"),
                          "rate": lg("rate"), "amount": lg("amount"),
                          "qty_fulfilled": lg("quantityfulfilled"),
                          "qty_billed": lg("quantitybilled")})

        fulfillments, invoices = [], []
        for ln in rec.iterfind('machine[@name="links"]/line'):
            lg = lambda n: _clean(ln.findtext(n))
            row = {"number": lg("tranid"), "date": lg("trandate"),
                   "status": lg("status"), "id": lg("id")}
            t = lg("type")
            if t == "Item Fulfillment":
                fulfillments.append(row)
            elif t == "Invoice":
                invoices.append(row)

        # Truck-scope rule (Jing 2026-08-17) only fires on multiple fulfillments,
        # so the extra per-IF fetch is spent only when it can change the verdict.
        if len(fulfillments) > 1:
            for f in fulfillments:
                f.update(self.fetch_fulfillment_load(f["id"]))

        edi_raw = g(F_EDI_INFO)
        edi_lines = _edi_address_lines(edi_raw)

        return {"so_number": _clean(rec.findtext("tranid")),
                "so_internal_id": tid,
                "date": g("trandate"), "status": g("status"),
                "customer_internal_id": g("entity"),
                "customer_name_on_so": g("entityname"),
                "po_number": g("otherrefnum"), "memo": g("memo"),
                "so_account_notes": g(F_SO_NOTES),
                "subtotal": g("subtotal") or "0",
                "discount_amount": g("discounttotal") or "0",
                "discount_item_id": g("discountitem"),
                "discount_item_name": g("discountitem_display") or (g("discountitem") and f"item #{g('discountitem')}") or "",
                "discount_rate": g("discountrate"),
                "shipping_cost": g("shippingcost") or "0",
                "misc_cost": g("althandlingcost") or "0",
                "total": g("total") or "0",
                "ship_address_text": g("shipaddress"),
                "bill_address_text": g("billaddress"),
                "edi_ship_lines": edi_lines,
                "edi_info_text": edi_raw,
                # whole-order material weight, lbs (Jing: this is the number the
                # 40,000 lb truck limit is compared against)
                "items_total_weight": g(F_TOTAL_WEIGHT),
                "lines": lines, "fulfillments": fulfillments, "invoices": invoices}

    def fetch_fulfillment_load(self, if_internal_id):
        """Pallet/package count and shipped weight of one Item Fulfillment.

        `package_count` is None when the IF could not be read — the engine then
        fails closed (NEEDS-REVIEW) instead of assuming a small load.
        """
        out = {"package_count": None, "package_weight": None,
               "shipment_weight": None, "weight_unit": ""}
        try:
            root = self._get_xml(
                f"/app/accounting/transactions/itemship.nl?id={if_internal_id}&xml=T")
        except Exception:
            return out
        rec = root.find("record")
        if rec is None or rec.get("recordType") != "itemfulfillment":
            return out
        pkgs = []
        for name in ("package", "packageups", "packagefedex", "packageusps"):
            pkgs += list(rec.iterfind(f'machine[@name="{name}"]/line'))
        out["package_count"] = len(pkgs)
        weights = [_clean(p.findtext("packageweight")) for p in pkgs]
        weights = [w for w in weights if w]
        if weights:
            try:
                out["package_weight"] = str(sum(float(w) for w in weights))
            except ValueError:
                pass
        out["shipment_weight"] = _clean(rec.findtext("onsaveshipmentweight"))
        out["weight_unit"] = (_clean(rec.findtext("baseweightunit"))
                              or _clean(rec.findtext("tranweightunit")))
        return out

    def fetch_customer(self, internal_id):
        root = self._get_xml(f"/app/common/entity/custjob.nl?id={internal_id}&xml=T")
        rec = root.find("record")
        if rec is None or rec.get("recordType") != "customer":
            raise LookupError(f"读取不到客户记录（内部 ID {internal_id}）")
        g = lambda n: _clean(rec.findtext(n))
        name = g("entityid")
        return {"internal_id": str(rec.get("id")), "code": re.sub(r"\s*-.*$", "", name),
                "name": name, "company_name": g("companyname"),
                "price_level": g("pricelevel_display") or g("pricelevel"),
                "price_level_id": g("pricelevel"),
                "account_notes": g(F_CUST_NOTES),
                "overseas": bool(re.search(r"-\s*[A-Za-z]", name))}


def login(timeout_s=600):
    import time
    src = PlaywrightSource(headless=False)
    src._ensure()
    page = src._ctx.new_page()
    page.goto(BASE + "/app/center/card.nl")
    print("请在弹出的浏览器窗口中登录 NetSuite（含 MFA）。每 5 秒自动检测一次…", flush=True)
    ok = False
    for _ in range(timeout_s // 5):
        time.sleep(5)
        try:
            src._get_xml("/app/common/entity/custjob.nl?id=486&xml=T")
            ok = True
            break
        except Exception:
            continue
    print("✅ 会话有效，登录状态已保存到 .browser-profile" if ok
          else "⛔ 超时：会话仍不可用", flush=True)
    src.close()
    return ok


if __name__ == "__main__":
    if "--login" in sys.argv:
        login()
    elif "--test" in sys.argv:
        so_num = sys.argv[sys.argv.index("--test") + 1]
        src = PlaywrightSource()
        so = src.fetch_so(so_num)
        cust = src.fetch_customer(so["customer_internal_id"])
        import json
        print(json.dumps({"so": so, "customer": cust}, ensure_ascii=False, indent=1))
        src.close()
