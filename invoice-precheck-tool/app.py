# -*- coding: utf-8 -*-
"""Invoice Pre-Check Tool — standalone local web app (stdlib only + openpyxl).

Run:  python app.py   ->  http://localhost:3100/
Daily scan fires at 15:00 local time while the app is running.
"""
import json
import os
import re
import threading
import traceback
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine
import sources

PORT = 3100
# 只监听本机。一轮扫描会往 NetSuite 写表单、往开票系统回填发票号，/api/override
# 还能把任意 SO 改判成"可直接开票"并署任意姓名 —— 这些都没有任何认证，绑
# 0.0.0.0 等于把它们开放给整个局域网。要给同事用就先加鉴权，别改这里。
BIND_HOST = "127.0.0.1"
HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, "reports")
os.makedirs(REPORTS, exist_ok=True)
SCAN_HOUR, SCAN_MINUTE = 15, 0

_lock = threading.Lock()
_override_lock = threading.Lock()
# One scan at a time. Two concurrent scans drive the same browser and both
# write to NetSuite and the billing app: the second backfill would find
# window.__origSave already set, then the first one restores DB.save mid-flight
# and every remaining markBilled fires a full-database POST — the exact server
# flooding the suppression exists to prevent.
_scan_lock = threading.Lock()

OVERRIDES = os.path.join(REPORTS, "overrides.json")
# Verdicts a human may set by hand. INVOICE-READY is the point of the feature:
# a reviewer who has decided the truck/load scope releases the SO for invoicing.
OVERRIDE_VERDICTS = ("INVOICE-READY", "NEEDS-REVIEW", "DO-NOT-INVOICE")

BILLING_APP = "http://192.168.0.1:3000"


def load_overrides():
    if not os.path.exists(OVERRIDES):
        return []
    try:
        with open(OVERRIDES, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_override(entry):
    """Append-only log of human verdict changes.

    Never rewrites the scan report itself: the machine result stays on record
    and the override sits beside it, so an audit can always see both.
    """
    with _override_lock:
        rows = load_overrides()
        rows.append(entry)
        tmp = OVERRIDES + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        os.replace(tmp, OVERRIDES)
    return entry


def check_environment():
    """Preflight: is everything the scan depends on actually available?

    Reported up front so a broken prerequisite is obvious immediately instead
    of showing up as 30 identical CHECK-INCOMPLETE rows.
    """
    out = {"ok": False, "chrome": False, "netsuite": False,
           "billing_login": None, "problems": []}
    try:
        urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5).read()
        out["chrome"] = True
    except Exception:
        out["problems"].append(
            "调试 Chrome 未运行（端口 9222）— 运行 补货系统\\scripts\\start_chrome_debug.ps1")
        return out

    try:
        src = sources.PlaywrightSource()
        try:
            src._get_text("/app/common/entity/custjob.nl?id=486&xml=T")
            out["netsuite"] = True
        except sources.NeedLoginError:
            out["problems"].append("NetSuite 未登录/会话过期 — 请在调试 Chrome 窗口里登录 NetSuite")
        except Exception as e:
            out["problems"].append(f"NetSuite 读取失败: {e}")
        # billing app login lives in sessionStorage (per tab)
        try:
            src._ensure()
            user = None
            for ctx in ([src._ctx] if src._ctx else []):
                for p in ctx.pages:
                    if p.url.startswith(BILLING_APP):
                        try:
                            user = p.evaluate(
                                "() => (typeof Auth !== 'undefined' && Auth.currentUser)"
                                " ? Auth.currentUser.fullName : null")
                        except Exception:
                            user = None
                        if user:
                            break
            out["billing_login"] = user
            if not user:
                out["problems"].append(
                    f"billing app 未登录 — 请在调试 Chrome 里打开 {BILLING_APP} 并登录"
                    "（自动回填需要，检查本身不受影响）")
        except Exception as e:
            out["problems"].append(f"billing app 状态未知: {e}")
        finally:
            src.close()
    except Exception as e:
        out["problems"].append(f"浏览器连接失败: {e}")
    out["ok"] = out["chrome"] and out["netsuite"] and bool(out["billing_login"])
    return out


def _backfilled_so_numbers():
    """SOs already invoiced & backfilled by this tool — never re-scan them."""
    p = os.path.join(REPORTS, "backfilled.json")
    if not os.path.exists(p):
        return set()
    try:
        with open(p, encoding="utf-8") as f:
            return {e["so_number"] for e in json.load(f)}
    except Exception:
        return set()


def normalize_so_numbers(raw):
    """'so9000004, 1398700\n SO9000005' -> ['SO9000004','SO9000006','SO9000005']"""
    out, seen = [], set()
    for tok in re.split(r"[\s,;]+", str(raw or "").strip()):
        if not tok:
            continue
        t = tok.upper()
        if t.isdigit():
            t = "SO" + t
        if not t.startswith("SO"):
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# 可以据此改表单的结论。写入名单必须显式列举结论，不能用"没有 has_invoice
# 这个键"来反推 —— 那个键只有走完检查的成功路径才会写上，抓取失败、会话过期、
# 工作簿加载失败的行根本没有它，于是"一项检查都没跑成功"的 SO 反而全部进了
# NetSuite 写入名单。
FORM_SWITCH_VERDICTS = ("INVOICE-READY", "NEEDS-REVIEW", "DO-NOT-INVOICE")


def form_switch_candidates(results):
    """挑出可以改表单的 SO。

    Jing 2026-08-18：结论如何都换表单（set_form 对已在目标表单的 SO 会跳过，
    重复扫描不产生写入）。但两种情况绝不写：已开票的 SO —— 发票已存在，表单
    没活可干；以及 CHECK-INCOMPLETE —— 这张单我们连读都没读明白，不该去改它。
    """
    return [r["so_number"] for r in results
            if r.get("overall") in FORM_SWITCH_VERDICTS and not r.get("has_invoice")]


class ScanBusy(RuntimeError):
    """Another scan is already running — never run two at once."""


# 扫描进度。一轮队列扫描要跑 3～15 分钟，页面在此期间只能干等，连"活着没有"
# 都看不出来；刷新一下更是彻底失联。这里把进度放在模块级，任何时候 GET
# /api/progress 都能接上正在跑的那一轮 —— 包括刚刷新完页面的浏览器。
_progress_lock = threading.Lock()
_progress = {"running": False, "mode": None, "phase": "空闲", "total": 0,
             "done": 0, "current_so": None, "started_at": None,
             "finished_at": None, "error": None}


def _set_progress(**kw):
    with _progress_lock:
        _progress.update(kw)


def scan_progress():
    with _progress_lock:
        return dict(_progress)


def run_scan(so_numbers=None):
    """Scan the billing queue, or an explicit SO list when so_numbers is given.

    Fail closed at every layer; one SO never stops the batch.
    Serialized: see _scan_lock.
    """
    if not _scan_lock.acquire(blocking=False):
        raise ScanBusy("已有一次扫描正在进行中 — 请等它结束后再试"
                       "（并发扫描会同时写 NetSuite 和开票系统）")
    _set_progress(running=True, mode="manual" if so_numbers is not None else "queue",
                  phase="准备中", total=0, done=0, current_so=None, finished_at=None,
                  error=None,
                  started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        return _run_scan_locked(so_numbers)
    except Exception as e:
        _set_progress(error=str(e))
        raise
    finally:
        # running 必须在 finally 里清掉：扫描中途抛异常时若留成 True，界面会
        # 永远显示"扫描中"，而且下一轮真正开始时也分辨不出来。
        _set_progress(running=False, phase="完成", current_so=None,
                      finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
        _scan_lock.release()


def _run_scan_locked(so_numbers=None):
    manual = so_numbers is not None
    report = {"started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "mode": "manual" if manual else "queue",
              "workbook": None, "queue_error": None, "skipped_invoiced": [], "results": []}
    _set_progress(phase="环境预检")
    report["preflight"] = check_environment()
    if report["preflight"]["problems"]:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] preflight: "
              + " | ".join(report["preflight"]["problems"]))
    try:
        wb = sources.load_reference_workbook()
        report["workbook"] = {"ok": True, "meta": wb["meta"]}
    except Exception as e:
        report["workbook"] = {"ok": False, "error": str(e)}
        wb = None

    if manual:
        # a manual test scan checks exactly what the user typed — no filtering
        queue = [{"so_number": s, "account_number": "", "po_number": ""} for s in so_numbers]
    else:
        _set_progress(phase="读取待开票队列")
        try:
            queue = sources.fetch_billing_queue()
        except Exception as e:
            report["queue_error"] = str(e)
            queue = []
        # drop SOs this tool has already invoiced/backfilled
        done = _backfilled_so_numbers()
        report["skipped_invoiced"] = [o["so_number"] for o in queue if o["so_number"] in done]
        queue = [o for o in queue if o["so_number"] not in done]

    _set_progress(phase="逐单检查", total=len(queue), done=0)
    ns = sources.PlaywrightSource()
    candidates = []
    try:
        _scan_queue(report, queue, ns, wb, candidates)
    finally:
        try:
            ns.close()
        except Exception:
            pass
        # 走 continue 分支的行不会执行循环末尾那次更新，这里统一收口
        _set_progress(done=len(report["results"]), current_so=None)

    # Auto-backfill: an SO already invoiced in NetSuite but still Unbilled in
    # the billing app has nothing to decide — invoice number and amount are
    # facts read straight off the invoice record. Queue mode only: a manual
    # test scan never writes anywhere.
    if candidates and not manual:
        _set_progress(phase="自动回填发票号")
        try:
            import backfill
            outcome = backfill.backfill(candidates, execute=True)
            done = {so for so, st, _ in outcome if st == "BILLED"}
            report["auto_backfill"] = [
                {"so_number": so, "status": st, "detail": det} for so, st, det in outcome]
            for r in report["results"]:
                if r["so_number"] in done:
                    r["backfilled"] = True
        except Exception as e:
            report["auto_backfill_error"] = str(e)

    # Auto form switch: an SO cleared for invoicing is put on the
    # "…with Rate with Close" form so the invoicing step closes the remaining
    # quantities. Queue mode only, and only the FORM field — set_form.py diffs
    # the whole record and flags any other change.
    if not manual:
        ready = form_switch_candidates(report["results"])
        skipped_invoiced_form = [r["so_number"] for r in report["results"]
                                 if r.get("has_invoice")]
        skipped_unchecked = [r["so_number"] for r in report["results"]
                             if r["so_number"] not in ready
                             and not r.get("has_invoice")]
        if skipped_invoiced_form:
            report["form_switch_skipped_invoiced"] = skipped_invoiced_form
        if skipped_unchecked:
            report["form_switch_skipped_unchecked"] = skipped_unchecked
        if ready:
            _set_progress(phase=f"切换 NetSuite 表单（{len(ready)} 张）")
            try:
                import set_form
                outcome = set_form.set_form(ready, execute=True)
                report["form_switch"] = [
                    {"so_number": so, "status": st, "detail": det} for so, st, det in outcome]
                done = {so for so, st, _ in outcome if st in ("CHANGED", "SKIP")}
                for r in report["results"]:
                    if r["so_number"] in done:
                        r["form_ready"] = True
            except Exception as e:
                report["form_switch_error"] = str(e)

    counts = {}
    for r in report["results"]:
        counts[r["overall"]] = counts.get(r["overall"], 0) + 1
    report["summary"] = {"queue": len(report["results"]), **counts}
    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    with _lock:
        path = os.path.join(REPORTS, datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
        with open(os.path.join(REPORTS, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    return report


def _collect_invoice_rows(ns, so, entry):
    """Read invoice number/amount off each linked Invoice record (facts only)."""
    from netsuite_scraper import _clean
    rows = []
    for inv in so.get("invoices", []):
        total, invdate = "", inv.get("date", "")
        try:
            root = ns._get_xml(
                f"/app/accounting/transactions/custinvc.nl?id={inv['id']}&xml=T")
            rec = root.find("record")
            if rec is not None:
                total = _clean(rec.findtext("total"))
                invdate = _clean(rec.findtext("trandate")) or invdate
        except Exception:
            continue
        if inv.get("number") and total:
            rows.append({"so_number": so["so_number"], "account": entry.get("account_number", ""),
                         "invoice_number": inv["number"], "invoice_amount": total,
                         "invoice_date": invdate})
    return rows


def _scan_queue(report, queue, ns, wb, candidates=None):
    for i, order in enumerate(queue):
        so_num = order["so_number"]
        _set_progress(current_so=so_num, done=i)
        entry = {"so_number": so_num, "account_number": order.get("account_number", ""),
                 "po_number": order.get("po_number", "")}
        if wb is None:
            entry["overall"] = "CHECK-INCOMPLETE"
            entry["reason"] = "基准工作簿不可用（未 ACTIVE 或格式不合法）— 无法检查"
            report["results"].append(entry)
            continue
        try:
            so = ns.fetch_so(so_num)
            cust = ns.fetch_customer(so["customer_internal_id"])
            # authoritative master-data match by Internal ID (plan Step 2)，
            # 再按 SO 日期挑出生效的那一行（方案 §4.2）。一个客户可以有多行
            # 生效期；没有生效行 / 多行同时生效都禁止开票。
            row, sel_err = engine.select_customer_row(
                wb["customers"].get(cust["internal_id"]),
                engine.parse_date(so.get("date")))
            if row is None:
                entry["overall"] = "DO-NOT-INVOICE"
                entry["reason"] = (f"客户内部 ID {cust['internal_id']}（{cust['name']}）："
                                   f"{sel_err} — 禁止开票")
                report["results"].append(entry)
                continue
            if not row["active"]:
                entry["overall"] = "DO-NOT-INVOICE"
                entry["reason"] = f"客户 {cust['name']} 在 Customers 工作表中被标记为不活跃（Active=FALSE）— 禁止开票"
                report["results"].append(entry)
                continue
            cust["handling"] = row["handling"]
            # workbook CAN Rule overrides the name-suffix-derived overseas flag
            if row["can_rule"] == "FORCE-CAN":
                cust["overseas"] = True
            elif row["can_rule"] == "NOT-CAN":
                cust["overseas"] = False
            # cross-check billing app account vs NetSuite customer (advisory)
            app_code = re.sub(r"\s*-.*$", "", entry["account_number"]).strip()
            if app_code and app_code != cust["code"]:
                entry["account_mismatch"] = (f"开票系统账号『{entry['account_number']}』与 NetSuite 客户"
                                             f"『{cust['name']}』不一致 — 需人工确认客户对应关系")
            result = engine.run_checks(so, cust, wb, wb["rules"])
            if entry.get("account_mismatch") and result["overall"] == "INVOICE-READY":
                result["overall"] = "NEEDS-REVIEW"
            entry.update(result)
            entry["has_invoice"] = bool(so.get("invoices"))
            # already invoiced in NetSuite but still Unbilled here -> backfill
            if candidates is not None and so.get("invoices"):
                rows = _collect_invoice_rows(ns, so, entry)
                if rows:
                    entry["backfill_pending"] = [r["invoice_number"] for r in rows]
                    candidates.extend(rows)
        except sources.NeedLoginError:
            entry["overall"] = "CHECK-INCOMPLETE"
            entry["reason"] = ("NetSuite 会话缺失/过期 — 运行 "
                              "`python netsuite_scraper.py --login` 登录一次")
        except Exception as e:
            entry["overall"] = "CHECK-INCOMPLETE"
            entry["reason"] = f"检查过程出错：{e}"
        report["results"].append(entry)
        _set_progress(done=len(report["results"]))


def _schedule_daily():
    def loop():
        while True:
            now = datetime.now()
            target = now.replace(hour=SCAN_HOUR, minute=SCAN_MINUTE, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            threading.Event().wait(wait)
            try:
                run_scan()
            except Exception:
                traceback.print_exc()
    t = threading.Thread(target=loop, daemon=True)
    t.start()


class Handler(BaseHTTPRequestHandler):
    _sent = False

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        # Guard against a second response on the same request: the fallback
        # error handlers below would otherwise write to an already-closed
        # socket, raise again, kill the worker thread and bury the log in
        # ConnectionAbortedError tracebacks.
        if self._sent:
            return
        self._sent = True
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass  # client went away mid-response; nothing useful left to do

    def log_message(self, *a):
        pass

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/status":
                out = {"scan_time": f"{SCAN_HOUR:02d}:{SCAN_MINUTE:02d} daily",
                       "netsuite_source": "CDP Chrome (:9222) · xml=T 结构化抓取"}
                try:
                    wb = sources.load_reference_workbook()
                    out["workbook"] = {"ok": True, "customers": len(wb["customers"]),
                                       "meta": wb["meta"]}
                except Exception as e:
                    out["workbook"] = {"ok": False, "error": str(e)}
                self._send(200, out)
            elif self.path == "/api/queue":
                try:
                    q = sources.fetch_billing_queue()
                    self._send(200, {"ok": True, "count": len(q), "queue": [
                        {k: o.get(k, "") for k in ("so_number", "account_number", "po_number",
                                                   "total_qty", "shipped_qty", "work_status")}
                        for o in q]})
                except Exception as e:
                    self._send(200, {"ok": False, "error": str(e)})
            elif self.path == "/api/report/latest":
                p = os.path.join(REPORTS, "latest.json")
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        self._send(200, f.read())
                else:
                    self._send(200, {"none": True})
            elif self.path == "/api/progress":
                self._send(200, scan_progress())
            elif self.path == "/api/preflight":
                self._send(200, check_environment())
            elif self.path == "/api/reports":
                files = sorted((f for f in os.listdir(REPORTS)
                                if re.match(r"^\d{8}-\d{6}\.json$", f)), reverse=True)
                self._send(200, {"reports": files})
            elif self.path.startswith("/api/report/file/"):
                name = self.path.rsplit("/", 1)[-1]
                if re.match(r"^\d{8}-\d{6}\.json$", name) and \
                        os.path.exists(os.path.join(REPORTS, name)):
                    with open(os.path.join(REPORTS, name), "rb") as f:
                        self._send(200, f.read())
                else:
                    self._send(404, {"error": "no such report"})
            elif self.path == "/api/overrides":
                self._send(200, load_overrides())
            elif self.path == "/api/backfilled":
                p = os.path.join(REPORTS, "backfilled.json")
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        self._send(200, f.read())
                else:
                    self._send(200, [])
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if self.path == "/api/run":
            try:
                self._send(200, run_scan())
            except ScanBusy as e:
                self._send(409, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif self.path == "/api/scan":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                sos = normalize_so_numbers(body.get("so_numbers", ""))
                if not sos:
                    self._send(400, {"error": "没有识别到 SO 号（例：SO9000004 或 1398022）"})
                    return
                self._send(200, run_scan(so_numbers=sos))
            except ScanBusy as e:
                self._send(409, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif self.path == "/api/override":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                so_num = str(body.get("so_number", "")).strip()
                verdict = str(body.get("overall", "")).strip()
                by = str(body.get("by", "")).strip()
                if not so_num or verdict not in OVERRIDE_VERDICTS:
                    self._send(400, {"error": f"参数不合法：SO 号必填，结论须为 {OVERRIDE_VERDICTS} 之一"})
                    return
                if not by:
                    self._send(400, {"error": "请先填写改判人姓名"})
                    return
                self._send(200, save_override({
                    "so_number": so_num, "overall": verdict, "by": by,
                    "note": str(body.get("note", "")).strip(),
                    "was": str(body.get("was", "")).strip(),
                    "report_file": str(body.get("report_file", "")).strip(),
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "date": datetime.now().strftime("%Y-%m-%d")}))
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    # Under pythonw.exe (autostart, no console) sys.stdout/stderr are None and
    # any print() would kill the process — send output to a log file instead.
    import sys
    if sys.stdout is None or sys.stderr is None:
        _log = open(os.path.join(REPORTS, "server.log"), "a", encoding="utf-8",
                    buffering=1)
        sys.stdout = sys.stderr = _log
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Invoice Pre-Check Tool -> "
          f"http://localhost:{PORT}/  (daily scan {SCAN_HOUR:02d}:{SCAN_MINUTE:02d})")
    _schedule_daily()
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()
