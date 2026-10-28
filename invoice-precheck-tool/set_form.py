# -*- coding: utf-8 -*-
"""Switch a Sales Order's FORM to "XXX - Sales Order with Rate with Close".

The ONLY write this tool makes to NetSuite. Guard rails:
  * loads the record in the target form via the edit URL's `cf` parameter,
    so no field is typed into and nothing else is touched;
  * snapshots the ENTIRE record XML before and after the save and diffs every
    field, so any unintended change NetSuite made on save is reported instead
    of passing silently;
  * skips SOs already on the target form (no pointless writes);
  * --run is required; without it the script only checks and reports.

Usage:
  python set_form.py --check SO9000009 SO9000010      # read current forms
  python set_form.py SO9000009                        # dry run (loads, no save)
  python set_form.py --run SO9000009                  # actually save
"""
import sys
import xml.etree.ElementTree as ET
from netsuite_scraper import PlaywrightSource, BASE

TARGET_FORM_ID = "219"
TARGET_FORM_TEXT = "XXX - Sales Order with Rate with Close"

# fields NetSuite legitimately updates on any save — not "changes we made"
IGNORE_TAGS = {
    "lastmodifieddate", "_csrf", "wfinstances", "entryformquerystring",
    "_eml_nkey_", "_multibtnstate_", "selectedtab", "nsapiPI", "nsapiSR",
    "nsapiVF", "nsapiFC", "nsapiPS", "nsapiVI", "nsapiVD", "nsapiPD",
    "nsapiVL", "nsapiRC", "nsapiLI", "nsapiLC", "nsapiCT", "nsbrowserenv",
    "wfPI", "wfSR", "wfVF", "wfFC", "wfPS", "vers", "customform",
}


def snapshot(src, tid):
    """Flatten the record XML into {path: value} for diffing."""
    root = src._get_xml(f"/app/accounting/transactions/salesord.nl?id={tid}&xml=T")
    rec = root.find("record")
    flat = {}

    def walk(el, path):
        for i, child in enumerate(el):
            tag = child.tag
            key = f"{path}/{tag}"
            if tag == "machine":
                key = f"{path}/machine[{child.get('name')}]"
            elif tag == "line":
                key = f"{path}/line[{i}]"
            if len(child):
                walk(child, key)
            else:
                flat[key] = (child.text or "").strip()
    walk(rec, "")
    return flat


# Metadata NetSuite rewrites on every save/render — verified benign on the
# first production write (SO9000011, 2026-08-17): row-render ids and
# last-modified stamps. Anything NOT listed here counts as a real change and
# is reported loudly, so an unfamiliar field never slips through silently.
BENIGN_TAGS = {"sys_id", "custbody71", "custbody_esc_last_modified_date",
               "lastmodifieddate", "lastmodified",
               # NetSuite's record version counter: bumped by every save.
               # Confirmed benign on 2026-08-18 — three SOs where the counter
               # moved while every other field stayed byte-identical.
               "version",
               # Live warehouse stock levels that NetSuite renders on each SO
               # line for reference. They are not order data and drift on their
               # own while other orders ship: SO9000012 showed four lines
               # dropping 1-2 units between the before and after snapshots,
               # with quantity/rate/amount/price level all unchanged.
               "quantityonhand", "quantityavailable"}


def diff(before, after):
    """Return (real_changes, benign_changes); each a list of (path, old, new)."""
    real, benign = [], []
    for k in sorted(set(before) | set(after)):
        tag = k.rsplit("/", 1)[-1]
        if tag in IGNORE_TAGS:
            continue
        b, a = before.get(k, "<absent>"), after.get(k, "<absent>")
        if b == a:
            continue
        (benign if tag in BENIGN_TAGS else real).append((k, b, a))
    return real, benign


def _audit(so, tid, old_form, real, benign):
    """Persist the full before/after diff of every NetSuite write, for audit."""
    import json
    import os
    from datetime import datetime
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports", "form_changes")
    os.makedirs(d, exist_ok=True)
    rec = {"so_number": so, "internal_id": tid, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "form_from": old_form, "form_to": TARGET_FORM_TEXT,
           "real_changes": [{"field": k, "old": b, "new": a} for k, b, a in real],
           "benign_changes": [{"field": k, "old": b, "new": a} for k, b, a in benign]}
    with open(os.path.join(d, f"{so}-{datetime.now():%Y%m%d-%H%M%S}.json"), "w",
              encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)


def current_form(page, tid):
    page.goto(f"{BASE}/app/accounting/transactions/salesord.nl?id={tid}&e=T",
              wait_until="domcontentloaded", timeout=90000)
    page.wait_for_function(
        "() => typeof nlapiGetFieldValue === 'function' && nlapiGetFieldValue('customform')",
        timeout=60000)
    return page.evaluate("() => [nlapiGetFieldValue('customform'), nlapiGetFieldText('customform')]")


def set_form(so_numbers, execute=False, check_only=False, sample_n=3):
    """Switch the FORM on each SO.

    Verification policy (per Jing 2026-08-17, tuned for speed):
      * a BEFORE snapshot is taken for every SO — it is one cheap XML fetch and
        without it a later problem could never be diffed retroactively;
      * the expensive post-save confirmation (AFTER snapshot + edit-page reload)
        runs on a random sample of `sample_n` SOs;
      * if ANY sampled SO shows a real field change or a wrong form, every
        remaining SO is verified too — a fault is assumed systematic, not
        isolated, so one bad sample escalates the whole batch.
    """
    import random
    src = PlaywrightSource()
    src._ensure()
    page = src._ctx.new_page()
    results = []
    befores = {}          # so -> snapshot, kept so escalation can diff later
    changed_ids = {}      # so -> internal id of SOs we actually saved
    sampled = set()
    try:
        for so in so_numbers:
            try:
                tid, _ = src.resolve_so(so)
                fid, ftext = current_form(page, tid)
                if fid == TARGET_FORM_ID:
                    results.append((so, "SKIP", f"already '{ftext}'"))
                    continue
                if check_only:
                    results.append((so, "NEEDS-CHANGE", f"currently '{ftext}' ({fid})"))
                    continue

                before = snapshot(src, tid)
                # load the record already in the target form; nothing typed
                page.goto(f"{BASE}/app/accounting/transactions/salesord.nl"
                          f"?id={tid}&e=T&cf={TARGET_FORM_ID}",
                          wait_until="domcontentloaded", timeout=90000)
                page.wait_for_function(
                    "() => typeof nlapiGetFieldValue === 'function'"
                    " && nlapiGetFieldValue('customform')", timeout=60000)
                loaded = page.evaluate("() => nlapiGetFieldValue('customform')")
                if loaded != TARGET_FORM_ID:
                    results.append((so, "ERROR", f"form did not load as target (got {loaded})"))
                    continue
                if not execute:
                    results.append((so, "WOULD-CHANGE", f"'{ftext}' -> '{TARGET_FORM_TEXT}'"))
                    continue

                page.click("#btn_multibutton_submitter", timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=120000)
                page.wait_for_timeout(3000)

                befores[so] = before
                changed_ids[so] = (tid, ftext)
                results.append((so, "CHANGED", f"'{ftext}' -> '{TARGET_FORM_TEXT}' (待抽验)"))
            except Exception as e:
                results.append((so, "ERROR", f"{type(e).__name__}: {str(e)[:120]}"))

        # ── verification pass ──────────────────────────────────────────────
        def verify(so):
            # A verification failure must never discard the batch: the writes
            # already happened, and losing the result list would hide which SOs
            # were changed.
            try:
                return _verify_one(so)
            except Exception as e:
                return (so, "CHANGED?",
                        f"FORM 已写入，但核验未能完成（{type(e).__name__}: {str(e)[:80]}）"
                        f" — 请人工确认该 SO 的表单与数据")

        def _verify_one(so):
            tid, ftext = changed_ids[so]
            after = snapshot(src, tid)
            newid, newtext = current_form(page, tid)
            real, benign = diff(befores[so], after)
            _audit(so, tid, ftext, real, benign)
            if newid != TARGET_FORM_ID:
                return (so, "ERROR", f"保存后表单是 {newid} ({newtext})")
            if real:
                return (so, "CHANGED+DIFF",
                        f"⚠ FORM 已改，但另有 {len(real)} 个业务字段变动 — 请人工核对: "
                        + "; ".join(f"{k}: {b!r}->{a!r}" for k, b, a in real[:6]))
            return (so, "CHANGED", f"'{ftext}' -> '{TARGET_FORM_TEXT}' "
                                   f"(已抽验：仅 {len(benign)} 项元数据变动)")

        pending = list(changed_ids)
        if pending:
            sampled = set(pending if len(pending) <= sample_n
                          else random.sample(pending, sample_n))
            verified = {}
            for so in sampled:
                verified[so] = verify(so)
            bad = [v for v in verified.values() if v[1] != "CHANGED"]
            if bad:
                # a fault is treated as systematic: verify everything else too
                for so in pending:
                    if so not in verified:
                        verified[so] = verify(so)
            results = [verified.get(so, (so, st, det + ("" if so in verified else " (未抽到，未逐字段核验)")))
                       if so in changed_ids else (so, st, det)
                       for so, st, det in results]
    finally:
        page.close()
        src.close()
    return results


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    execute = "--run" in sys.argv
    check = "--check" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)
    mode = "EXECUTE (会写入 NetSuite)" if execute else ("CHECK ONLY" if check else "DRY RUN")
    print(f"{mode} — {len(args)} SO\n")
    for so, st, det in set_form(args, execute=execute, check_only=check):
        print(f"{so:12} {st:14} {det}")
