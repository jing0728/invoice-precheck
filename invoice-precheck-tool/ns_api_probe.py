# -*- coding: utf-8 -*-
"""一次性探针：验证 NetSuite TBA 令牌能否取全预检引擎需要的字段。

这是 spike 用的临时脚本，不是正式代码。结论出来后要么删掉，要么把验证过的
取数逻辑整理成 sources.RestApiSource。

用法:
    python ns_api_probe.py <SO号>           # 例: python ns_api_probe.py 1398022

凭据文件 ns_api_credentials.json 按顺序在这几处找（第一个找到的生效）:
    1. 脚本所在目录        2. 当前工作目录
    3. ~/ns_api_credentials.json               4. ~/.ns_api_credentials.json
也可用 NS_CRED_FILE 指定绝对路径。内容:
    {
      "account_id":      "1234567",
      "consumer_key":    "...",
      "consumer_secret": "...",
      "token_id":        "...",
      "token_secret":    "..."
    }
也可改用环境变量 NS_ACCOUNT_ID / NS_CONSUMER_KEY / NS_CONSUMER_SECRET /
NS_TOKEN_ID / NS_TOKEN_SECRET。脚本自己不打印任何密钥。
"""
import base64
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CRED_NAME = "ns_api_credentials.json"
# 按顺序找：脚本旁边 -> 当前目录 -> 家目录 -> 环境变量 NS_CRED_FILE 指定的路径。
# 这样脚本拷到 WSL/Linux 上也能直接跑，不用改代码。
CRED_PATHS = [
    Path(__file__).resolve().parent / CRED_NAME,
    Path.cwd() / CRED_NAME,
    Path.home() / CRED_NAME,
    Path.home() / ("." + CRED_NAME),
]


def load_creds():
    keys = ("account_id", "consumer_key", "consumer_secret", "token_id", "token_secret")
    paths = list(CRED_PATHS)
    if os.environ.get("NS_CRED_FILE"):
        paths.insert(0, Path(os.environ["NS_CRED_FILE"]).expanduser())

    src, c = None, {}
    for p in paths:
        if p.is_file():
            c = json.loads(p.read_text(encoding="utf-8"))
            src = p
            break
    if src is None:
        c = {k: os.environ.get("NS_" + k.upper(), "") for k in keys}
        src = "环境变量 NS_*"

    missing = [k for k in keys if not str(c.get(k, "")).strip()]
    if missing:
        raise SystemExit(
            f"缺少凭据字段 {missing}（来源：{src}）\n"
            f"请在下列任一位置放一个 {CRED_NAME}，或设置 NS_CRED_FILE 指向它：\n  "
            + "\n  ".join(str(p) for p in paths)
            + "\n或直接设置环境变量 NS_ACCOUNT_ID / NS_CONSUMER_KEY / "
              "NS_CONSUMER_SECRET / NS_TOKEN_ID / NS_TOKEN_SECRET")
    print(f"（凭据来源：{src}）")
    return {k: str(c[k]).strip() for k in keys}


def _q(s):
    """OAuth 百分号编码（RFC5849：只有 -._~ 和字母数字不转义）。"""
    return urllib.parse.quote(str(s), safe="-._~")


def _auth_header(creds, method, url):
    """TBA = OAuth 1.0a, HMAC-SHA256, realm 是 account id。

    query string 里的参数必须参与签名，body（JSON）不参与。
    """
    parts = urllib.parse.urlsplit(url)
    base_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
    oauth = {
        "oauth_consumer_key": creds["consumer_key"],
        "oauth_token": creds["token_id"],
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": str(int(time.time())),
        "oauth_nonce": "%030x" % random.randrange(16 ** 30),
        "oauth_version": "1.0",
    }
    all_params = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    all_params.update(oauth)
    norm = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(all_params.items()))
    base = f"{method.upper()}&{_q(base_url)}&{_q(norm)}"
    key = f"{_q(creds['consumer_secret'])}&{_q(creds['token_secret'])}"
    sig = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha256).digest()).decode()
    oauth["oauth_signature"] = sig
    inner = ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(oauth.items()))
    return f'OAuth realm="{_q(creds["account_id"].upper())}", {inner}'


def call(creds, method, path, body=None, extra_headers=None):
    host = f"https://{creds['account_id'].lower().replace('_', '-')}.suitetalk.api.netsuite.com"
    url = host + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("Authorization", _auth_header(creds, method, url))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:2000]}


def suiteql(creds, sql, limit=100):
    return call(creds, "POST", f"/services/rest/query/v1/suiteql?limit={limit}",
                {"q": sql}, {"Prefer": "transient"})


# 引擎真正需要的字段 —— 探针就是来核对这张表能不能取全的
NEED_SO = {
    "tranId": "SO 号", "tranDate": "SO 日期", "status": "状态",
    "entity": "客户内部 ID", "otherRefNum": "PO 号", "memo": "备注",
    "custbody4": "SO Account Notes", "custbody36": "EDI INFO（含地址块）",
    "custbody_items_total_weight": "整单材料重量（40000 lb 整车规则）",
    "subtotal": "小计", "discountTotal": "折扣金额", "discountItem": "折扣项",
    "discountRate": "折扣率", "shippingCost": "运费", "altHandlingCost": "杂费",
    "total": "总额", "shipAddress": "发货地址", "billAddress": "账单地址",
}
NEED_LINE = {"item": "货品", "quantity": "数量", "price": "Price Level",
             "rate": "单价（<=1000 检查）", "amount": "行金额",
             "quantityFulfilled": "已发数量", "quantityBilled": "已开票数量"}
NEED_CUST = {"entityId": "客户名", "companyName": "公司名",
             "priceLevel": "默认 Price Level", "custentity4": "客户 Account Notes"}


def check(label, obj, need):
    print(f"\n--- {label} ---")
    ok = miss = 0
    for f, desc in need.items():
        v = obj.get(f) if isinstance(obj, dict) else None
        if isinstance(v, dict):
            v = v.get("refName") or v.get("id") or v
        if v in (None, "", [], {}):
            print(f"  [缺] {f:32} {desc}")
            miss += 1
        else:
            s = str(v).replace("\n", "/")[:70]
            print(f"  [有] {f:32} {desc:24} = {s}")
            ok += 1
    print(f"  -> 取到 {ok} 项，缺 {miss} 项")
    return miss


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python ns_api_probe.py <SO号>")
    so_number = sys.argv[1].strip()
    creds = load_creds()
    problems = 0

    # (1) SuiteQL 能不能跑 + 按 tranid 精确定位 SO（替代现在的全局搜索页正则）
    print("(1) SuiteQL 按 SO 号定位 internal id")
    st, r = suiteql(creds, "SELECT id, tranid, trandate, status, entity, foreigntotal "
                           "FROM transaction "
                           f"WHERE type = 'SalesOrd' AND tranid = '{so_number}'")
    print(f"  HTTP {st}")
    so_id, has_suiteql = None, (st == 200)
    if has_suiteql:
        rows = r.get("items", [])
        print(f"  命中 {len(rows)} 行：{json.dumps(rows, ensure_ascii=False)[:400]}")
        if rows:
            so_id = rows[0]["id"]
    else:
        print("  [拒绝] SuiteQL 不可用：", json.dumps(r, ensure_ascii=False)[:400])
        problems += 1
        # SuiteQL 和记录 API 是两套权限。SuiteQL 不通时退回记录 API 查单号，
        # 这样字段覆盖报告照样能出，不至于什么都验不成。
        print("  → 退回用记录 API 按 tranId 查")
        q = urllib.parse.quote(f'tranId IS "{so_number}"', safe="")
        st2, r2 = call(creds, "GET",
                       f"/services/rest/record/v1/salesOrder?q={q}&limit=5")
        print(f"    HTTP {st2}  {json.dumps(r2, ensure_ascii=False)[:300]}")
        items = (r2.get("items") or []) if st2 == 200 else []
        if items:
            so_id = items[0].get("id")
    if not so_id:
        print("  [致命] 两条路都定位不到这个 SO —— 权限不足，或换一个真实存在的 SO 号")
        return 1
    print(f"  → internal id = {so_id}")

    # (2) REST 记录 API 取整张 SO（含自定义字段和行）
    print(f"\n(2) REST 取 salesOrder/{so_id}")
    st, so = call(creds, "GET",
                  f"/services/rest/record/v1/salesOrder/{so_id}?expandSubResources=true")
    print(f"  HTTP {st}")
    if st != 200:
        print("  [致命] 记录 API 不通：", json.dumps(so, ensure_ascii=False)[:800])
        so = {}
        problems += 1
    else:
        problems += check("SO 头部字段", so, NEED_SO)
        items = (so.get("item") or {}).get("items") or []
        print(f"\n--- SO 行（共 {len(items)} 行）---")
        if items:
            problems += check("第一行", items[0], NEED_LINE)
            print(f"  行上可见字段：{sorted(items[0].keys())}")
        else:
            print("  [缺] 取不到行 —— Price Level / 单价 / 已发数量三项检查都依赖它")
            problems += 1

    # (3) 关联单据（Item Fulfillment / Invoice）—— 整车规则和已开票判断都靠它
    print("\n(3) 关联单据（IF / Invoice）")
    st, r = suiteql(creds, "SELECT nt.nextdoc, t.tranid, t.type, t.trandate, t.status "
                           "FROM nexttransactionlink nt "
                           "JOIN transaction t ON t.id = nt.nextdoc "
                           f"WHERE nt.previousdoc = {so_id}")
    print(f"  HTTP {st}  {json.dumps(r.get('items', r), ensure_ascii=False)[:600]}")
    if st != 200 or not r.get("items"):
        print("  [警告] 关联单据取不到（这个 SO 也可能本来就没发货）—— 需要换有发货的 SO 复验")
        problems += 1

    # (4) 客户记录
    ent = so.get("entity") if isinstance(so, dict) else None
    cust_id = ent.get("id") if isinstance(ent, dict) else rows[0].get("entity")
    print(f"\n(4) REST 取 customer/{cust_id}")
    st, cust = call(creds, "GET", f"/services/rest/record/v1/customer/{cust_id}")
    print(f"  HTTP {st}")
    if st != 200:
        print("  [致命] 客户记录不通：", json.dumps(cust, ensure_ascii=False)[:600])
        problems += 1
    else:
        problems += check("客户字段", cust, NEED_CUST)

    print("\n" + "=" * 60)
    print("全部取到，可以直接写 RestApiSource" if problems == 0
          else f"有 {problems} 处缺口 —— 缺的那部分回退抓取器，其余走 API")
    return 0


if __name__ == "__main__":
    sys.exit(main())
