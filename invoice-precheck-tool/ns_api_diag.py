# -*- coding: utf-8 -*-
"""一次性权限诊断：TBA 签名已经通过，这里逐项试探这个角色到底能做什么。

和 ns_api_probe.py 一样是 spike 临时脚本，结论出来后删掉。
用法: python ns_api_diag.py [SO号]
"""
import json
import sys
import urllib.parse

from ns_api_probe import call, load_creds, suiteql


def show(label, st, body, keep=260):
    if st == 200:
        s = json.dumps(body, ensure_ascii=False)[:keep]
        print(f"  [通过] {label:46} HTTP {st}  {s}")
        return True
    detail = ""
    for d in (body.get("o:errorDetails") or []):
        detail = d.get("detail", "")
        break
    detail = detail or json.dumps(body, ensure_ascii=False)[:keep]
    print(f"  [拒绝] {label:46} HTTP {st}  {detail[:keep]}")
    return False


def main():
    so_number = sys.argv[1].strip() if len(sys.argv) > 1 else "SO9000008"
    creds = load_creds()

    print("A. SuiteQL 到底能不能用（先用一句不碰任何业务表的查询）")
    ok_dual = show("SELECT 1 FROM dual", *suiteql(creds, "SELECT 1 AS n FROM dual"))

    print("\nB. SuiteQL 各张表的表级权限")
    for label, sql in [
        ("customer 表", "SELECT id, entityid FROM customer"),
        ("transaction 表", "SELECT id, tranid FROM transaction WHERE rownum = 1"),
        ("transactionline 表", "SELECT transaction, item FROM transactionline WHERE rownum = 1"),
        ("nexttransactionlink 表", "SELECT previousdoc, nextdoc FROM nexttransactionlink WHERE rownum = 1"),
        ("item 表", "SELECT id, itemid FROM item WHERE rownum = 1"),
    ]:
        show(label, *suiteql(creds, sql, limit=1))

    print("\nC. REST 记录 API（与 SuiteQL 是两套权限，这条通了就不必依赖 SuiteQL）")
    st, r = call(creds, "GET", "/services/rest/record/v1/salesOrder?limit=1")
    ok_rec = show("列出 salesOrder", st, r)

    so_id = None
    q = urllib.parse.quote(f'tranId IS "{so_number}"', safe="")
    st, r = call(creds, "GET",
                 f"/services/rest/record/v1/salesOrder?q={q}&limit=5")
    if show(f'按 tranId 查 {so_number}', st, r):
        items = r.get("items") or []
        if items:
            so_id = items[0].get("id")
            print(f"       -> 命中 internal id = {so_id}")
        else:
            print(f"       -> 查询通了但没命中；换个 SO 号，或前缀不该带 SO")

    if so_id:
        st, so = call(creds, "GET",
                      f"/services/rest/record/v1/salesOrder/{so_id}?expandSubResources=true")
        if show(f"取整张 salesOrder/{so_id}", st, so, keep=120):
            print(f"       -> 头部可见字段共 {len(so)} 个")
            cb = sorted(k for k in so if k.startswith("custbody"))
            print(f"       -> 自定义字段 custbody*: {cb if cb else '一个都没有（要开 expandSubResources 或权限不足）'}")
            for f in ("custbody36", "custbody4", "custbody_items_total_weight"):
                print(f"       -> {f}: {'有' if so.get(f) not in (None, '', [], {}) else '缺'}")

    print("\nD. 其它记录类型")
    for label, path in [
        ("customer", "/services/rest/record/v1/customer?limit=1"),
        ("itemFulfillment", "/services/rest/record/v1/itemFulfillment?limit=1"),
        ("invoice", "/services/rest/record/v1/invoice?limit=1"),
    ]:
        show(label, *call(creds, "GET", path))

    print("\n" + "=" * 66)
    if not ok_dual:
        print("SuiteQL 整体被拒 -> 角色缺 “SuiteAnalytics Workbook” 权限（Setup 类）")
    if not ok_rec:
        print("REST 记录 API 也被拒 -> 角色缺 “REST Web Services”（Setup）或 Sales Order 列表权限")
    if ok_rec:
        print("REST 记录 API 可用 -> 不必等 SuiteQL，取数可以全走记录 API")


if __name__ == "__main__":
    main()
