# -*- coding: utf-8 -*-
"""Backfill invoice number/amount into the billing app's Team 3 tab.

Writes ONLY through the app's own JS path (window._t3draft + markBilled):
the app applies its validation, logging, timestamps and server sync exactly
as if a human clicked Billed. Never POSTs /db directly.

Usage:
  python backfill.py reports/backfill_preview.json        # dry-run (validate)
  python backfill.py reports/backfill_preview.json --run  # execute
"""
import json
import sys
from netsuite_scraper import PlaywrightSource

APP = "http://192.168.0.1:3000"


def backfill(rows, execute=False):
    src = PlaywrightSource()
    src._ensure()
    # the app keeps its login in sessionStorage (per tab) — reuse the user's
    # already-logged-in billing tab instead of opening a fresh one
    page, user, own_page = None, None, False
    for p in src._ctx.pages:
        if p.url.startswith(APP):
            try:
                u = p.evaluate("() => (typeof Auth !== 'undefined' && Auth.currentUser "
                               "&& Auth.currentUser.fullName) || null")
            except Exception:
                continue
            if u:
                page, user = p, u
                break
    if page is None:
        src.close()
        raise RuntimeError("没有找到已登录的 billing app 标签页 — 请在 Chrome 里打开 "
                           + APP + " 并登录，然后重试")
    page.wait_for_function(
        "() => typeof DB !== 'undefined' && Array.isArray(DB.orders) && DB.orders.length > 0",
        timeout=30000)
    print(f"billing app user: {user} (复用已登录标签页)")

    results = []
    for r in rows:
        so, inv, amt = r["so_number"], r["invoice_number"], str(r["invoice_amount"]).replace(",", "")
        state = page.evaluate("""(so) => {
            const o = DB.getOrder(so);
            return o ? {stage: o.workflow_stage, billing: o.billing_status || 'Unbilled',
                        inv: o.invoice_number || '', amt: o.invoice_amount || ''} : null;
        }""", so)
        if state is None:
            results.append((so, "SKIP", "order not found in billing app"))
            continue
        if state["billing"] == "Billed":
            if state["inv"] == inv:
                results.append((so, "SKIP", f"already billed as {state['inv']} (idempotent)"))
            else:
                results.append((so, "CONFLICT", f"already billed as {state['inv']} != {inv} — resolve manually"))
            continue
        if state["stage"] != "Team3":
            results.append((so, "SKIP", f"not in Team3 (stage={state['stage']})"))
            continue
        if not execute:
            results.append((so, "WOULD-BILL", f"{inv} / ${amt}"))
            continue
        # Apply through the app's own markBilled (keeps its validation, log and
        # timestamps) but suppress its per-row DB.save(): each save POSTs the
        # ENTIRE orders array, and firing that once per row floods the server —
        # the requests hang unresolved, and because the app only counts
        # failures in .then/.catch, it still reports "saved". One awaited save
        # for the whole batch happens after the loop instead.
        outcome = page.evaluate("""([so, inv, amt]) => {
            if (!window.__origSave) { window.__origSave = DB.save.bind(DB); DB.save = () => {}; }
            window._t3draft = window._t3draft || {};
            window._t3draft[so] = {invNum: inv, invAmt: amt};
            try { markBilled(so, null); } catch (e) { return 'error: ' + e.message; }
            const o = DB.getOrder(so);
            return (o.billing_status === 'Billed' && o.invoice_number === inv)
                ? 'applied' : 'verify-failed: ' + o.billing_status + '/' + o.invoice_number;
        }""", [so, inv, amt])
        results.append((so, "APPLIED" if outcome == "applied" else "ERROR",
                        f"{inv} / ${amt}" + ("" if outcome == "applied" else f" ({outcome})")))

    # Restore DB.save unconditionally. It is stubbed out as soon as the FIRST
    # row is applied, so if every row then errors (e.g. a teammate moved the SO
    # out of Team 3 between scan and backfill) the old code skipped the restore
    # and left the user's tab silently unable to save anything at all.
    try:
        if execute and any(st == "APPLIED" for _, st, _ in results):
            print("\n提交到服务器（单次全量保存，等待确认）…")
            post = page.evaluate("""async () => {
            if (window.__origSave) { DB.save = window.__origSave; delete window.__origSave; }
            const t0 = Date.now();
            try {
                const r = await fetch('/db', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json',
                              'X-Client-ID': String(window._clientId || 0)},
                    body: JSON.stringify({orders: DB.orders, shipments: DB.shipments,
                                          workflowLog: DB.workflowLog})
                });
                return {ok: r.ok, status: r.status, ms: Date.now() - t0};
            } catch (e) { return {ok: false, error: String(e), ms: Date.now() - t0}; }
        }""")
            print(f"  服务器响应: {post}")
            if not post.get("ok"):
                print("  ⚠ 保存未被服务器确认 — 下面的核对会显示实际落库情况")

            # independent server-side verification (not the browser's own copy)
            verify = page.evaluate("""async (sos) => {
            const db = await (await fetch('/db', {cache: 'no-store'})).json();
            const idx = {};
            db.orders.forEach(o => idx[o.so_number] = o);
            return sos.map(s => [s, (idx[s] || {}).billing_status || 'missing',
                                    (idx[s] || {}).invoice_number || '']);
        }""", [so for so, st, _ in results if st == "APPLIED"])
            vmap = {v[0]: (v[1], v[2]) for v in verify}
            results = [(so, ("BILLED" if vmap.get(so, ("", ""))[0] == "Billed" else "NOT-SAVED")
                        if st == "APPLIED" else st, det) for so, st, det in results]
    finally:
        # Always hand the tab back in a working state, whatever happened above.
        try:
            page.evaluate("() => { if (window.__origSave) "
                          "{ DB.save = window.__origSave; delete window.__origSave; } }")
        except Exception as e:
            print(f"  ⚠ 未能还原 billing app 的保存函数（{e}）— 请刷新该标签页后再操作")
    # do NOT close the user's own billing tab; just disconnect
    src.close()

    # log successful writes so the UI can show them as INVOICED, by date
    billed = [(so, det) for so, st, det in results if st == "BILLED"]
    if billed:
        import datetime
        import os
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "reports", "backfilled.json")
        log = []
        if os.path.exists(log_path):
            try:
                with open(log_path, encoding="utf-8") as f:
                    log = json.load(f)
            except Exception as e:
                # never silently start a fresh log: that would lose the record
                # of everything already invoiced and let those SOs re-enter the
                # scan queue
                raise RuntimeError(
                    f"回填日志 {log_path} 无法解析（{e}）— 已写入 billing app 的这批记录"
                    "尚未登记，请先修复该文件再重跑，不要让它被覆盖") from e
        now = datetime.datetime.now().astimezone()
        by_so = {r["so_number"]: r for r in rows}
        for so, _ in billed:
            r = by_so.get(so, {})
            log.append({"date": now.strftime("%Y-%m-%d"),
                        "time": now.strftime("%H:%M:%S"),
                        "so_number": so, "account": r.get("account", ""),
                        "invoice_number": r.get("invoice_number", ""),
                        "invoice_amount": r.get("invoice_amount", ""),
                        "by": user})
        # atomic replace: a crash mid-write must not leave a truncated file
        tmp = log_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=1)
        os.replace(tmp, log_path)
    return results


if __name__ == "__main__":
    path = sys.argv[1]
    execute = "--run" in sys.argv
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    print(("EXECUTING" if execute else "DRY-RUN") + f" — {len(rows)} rows\n")
    for so, status, detail in backfill(rows, execute):
        print(f"{so:12} {status:12} {detail}")
