# -*- coding: utf-8 -*-
"""Deterministic invoice pre-check engine. No LLM, no network.

All functions are pure: they take normalized data dicts and return
per-check results {status, evidence} plus an overall verdict.
Statuses: PASS / FAIL / NEEDS-REVIEW / N/A
Overall:  INVOICE-READY / DO-NOT-INVOICE / NEEDS-REVIEW / CHECK-INCOMPLETE
"""
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

TWO = Decimal("0.01")


def D(x):
    """Parse a currency/number string ('1,864.73', '$149.18', '(149.18)') to Decimal."""
    if isinstance(x, Decimal):
        return x
    s = str(x).strip().replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    if s in ("", "-"):
        return Decimal("0")
    return Decimal(s)


def money(x, mode=ROUND_HALF_UP):
    return D(x).quantize(TWO, rounding=mode)


def parse_date(s):
    """Parse '8/12/2026', '2026-08-12', '12/01/2025' to date.

    也接受 Excel 交回来的 date/datetime 对象：工作簿里这些列现在存的是文本，
    但只要有人在 Excel 里正常编辑一次就会变成日期型单元格，openpyxl 那时给
    的是 datetime。不认的话 parse_date 返回 None，生效期判断会静默失效 ——
    所有行都会被当成"永远生效"。
    """
    if isinstance(s, datetime):   # datetime 是 date 的子类，必须先判
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ── 客户主数据的生效期（方案 §4.2） ────────────────────────────────────────

def _effective_period(row):
    """(start, end, error)。某一端为空表示这一端不限。"""
    bounds = []
    for key, label in (("effective_from", "生效起始"), ("effective_to", "生效截止")):
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            bounds.append(None)
            continue
        d = parse_date(raw)
        if d is None:
            return None, None, f"{label}日期无法解析：'{raw}'"
        bounds.append(d)
    start, end = bounds
    if start and end and start > end:
        return None, None, f"生效期起止颠倒：{start} 晚于 {end}"
    return start, end, None


def select_customer_row(rows, so_date):
    """从同一个客户内部 ID 的若干行里，挑出在 SO 日期生效的那一行。

    返回 (row, error)。error 非空即禁止开票 —— 方案 §4.2：没有生效行、
    多行同时生效，都是 DO-NOT-INVOICE。

    刻意做成按客户逐个失败关闭，而不是在加载工作簿时整体报错：一个客户的
    生效期写错，不该让当天整个队列都跑不了。（此前更糟 —— 同一客户写两行
    会触发"重复内部 ID"，直接让整个工作簿加载失败。）
    """
    rows = list(rows or [])
    if not rows:
        return None, "客户主数据里没有这个客户"
    if so_date is None:
        return None, "SO 日期读不到，无法确定该用哪一行客户主数据"
    hits = []
    for r in rows:
        start, end, err = _effective_period(r)
        if err:
            return None, f"客户主数据的{err}"
        if start and so_date < start:
            continue
        if end and so_date > end:
            continue
        hits.append(r)
    if not hits:
        periods = "、".join(
            f"{r.get('effective_from') or '不限'}~{r.get('effective_to') or '不限'}"
            for r in rows)
        return None, (f"客户主数据里没有在 SO 日期 {so_date} 生效的行"
                      f"（现有生效期：{periods}）")
    if len(hits) > 1:
        return None, (f"客户主数据有多行（{len(hits)} 行）在 SO 日期 {so_date} "
                      f"同时生效，生效期重叠 — 需先修工作簿")
    return hits[0], None


# ── Discount evidence parsing (XXX text conventions) ───────────────────────
# Recognized patterns; anything discount-like that does not fully match is
# reported as unparsed -> NEEDS-REVIEW (fail closed, never guess).

_PCT = r"(\d+(?:\.\d+)?)\s*[%％]"
PATTERNS = [
    # "*STK Orders: 7%=(3%WTY+4%Vol), THEN 5%COD)" — Jing 2026-08-17: the
    # THEN means sequential compounding: 1-(1-7%)(1-5%) = 11.65%.
    ("STKCOMBO", re.compile(_PCT + r"\s*=?\s*\([^)]*\)\s*,?\s*THEN\s+" + _PCT, re.I)),
    # "5% Disc-CAN, then 3% Discount in Lieu of Warranty." — 同一条 THEN 规则，
    # 只是没有 "=(拆分)" 那一段（客户 3011 -CAN / 3000 / 2973 -CSAM）。此前只认
    # 上面那种写法，这类备注里前面那个折扣被静默丢掉。
    ("SEQ", re.compile(_PCT + r"[^%\n]{0,40}?,?\s*then\s+" + _PCT, re.I)),
    # "13% Disc =(3% Lieu of Warranty +10% Discount per June)" / "8% Dis =3% WTY+5%"
    # "BUY & SELL ORDER - EXTRA 5% DISCOUNT" —— 与常设折扣相乘的额外折扣
    # （Jing 2026-08-18：5% 与 9% 相乘正好是 NetSuite 上的 13.55%）。
    # 必须排在 COMBINED 前面：真实 memo 是
    # 'BUY & SELL ORDER - EXTRA 5% DI SCOUNT. 9% Discount= 6% + 3% …' 一整行，
    # COMBINED 命中"9% Discount="后会 break，排在后面的 EXTRA 就再也轮不到。
    # 拼写要容忍 NetSuite 里那个 "DI SCOUNT"（词中间多一个空格）。
    ("EXTRA", re.compile(r"EXTRA\s*" + _PCT + r"\s*DI\s*SC(?:OUNT)?", re.I)),
    # "10%=3% Warranty + 7%COD Disc" / "8% Discount (3% ... + 5% per Agreement)"
    # XXX convention: the LEADING percentage is the stated combined total
    # (additive); parse it as one discount and ignore the breakdown after =/(.
    ("COMBINED", re.compile(_PCT + r"\s*(?:Disc?(?:ount)?\.?)?\s*[=(](?=[^)]*%)", re.I)),
    # "8% LTL order discount effect date: 12/01/2025" / "(Ending on 11/30/2025)"
    ("LTL", re.compile(_PCT + r"\s*LTL\s+order\s+discount", re.I)),
    # warranty family, all variants: "3% Discount in Lieu of Warranty",
    # "3% in lieu of warranty", "3% lieu of warranty", "3% warranty off
    # invoice", "2% in Lieu of Warranty Discount" (Jing: treat all the same)
    ("WARRANTY", re.compile(
        _PCT + r"[^%\n]{0,25}?(?:lieu\s+of\s+warranty|warranty\s+off\s+invoice)", re.I)),
    # "10% off over $3,000"
    ("OVER", re.compile(_PCT + r"\s*off\s+over\s+\$?\s*([\d,]+)", re.I)),
    # "5% Disc on +64 Pcs of PN#1333404, #1333270 L&R" — item-scoped threshold
    # discount. Rule (Jing 2026-08-17): threshold met by the listed PNs
    # together or individually -> 5% on those PNs only; other items get none.
    ("ITEMSCOPE", re.compile(
        _PCT + r"\s*Disc(?:ount)?\.?\s*on\s*\+?\s*(\d+)\s*Pcs(?:\s*of)?\s*((?:PN)?#[\d#,\sLR&/]+)",
        re.I)),
]
# 条件性折扣：认得出来，但工具不自动套用，一律交人工。
# SEQ（"先 X% 再 Y%"）在这里：这种写法目前只出现在 3011 -CAN / 3000 /
# 2973 -CSAM 上，而这几个都不是 XXX 这边的账号（Jing 2026-08-18），口径从未
# 确认过。已确认的只有 STKCOMBO 那一种（7%=(...) THEN 5%，客户 2936 -MEX，
# 8/17 用 NetSuite 截图核过），所以只有它自动算。
CONDITIONAL_REASONS = {"FREIGHT", "SEQ"}
_CONDITIONAL_REVIEW_TEXT = {
    "SEQ": "『先 X% 再 Y%』式的连续折扣，口径未确认（目前只见于非 XXX 账号），"
           "工具不自动计算，需人工确认实际折扣比例",
}
_CONDITIONAL_REVIEW_DEFAULT = "条件性折扣（取决于订单类型），工具不自动套用，需人工确认是否适用"
_PN_TOKEN = re.compile(r"#\s*(\d+)\s*(L\s*&\s*R|L\s*/\s*R)?", re.I)


def _expand_pns(text):
    """'PN#1333404, #1333270 L&R' -> ['1333404', '1333270L', '1333270R']"""
    out = []
    for m in _PN_TOKEN.finditer(text):
        num, lr = m.group(1), m.group(2)
        if lr:
            out += [num + "L", num + "R"]
        else:
            out.append(num)
    return out
_EFFECT = re.compile(r"effect\s*(?:ive)?\s*(?:date|as of)?\s*:?\s*([\d/.\-]+)", re.I)
_ENDING = re.compile(r"ending\s*(?:on)?\s*:?\s*([\d/.\-]+)", re.I)
_DISCOUNTISH = re.compile(r"[%％]|\bdisc\b|\bdiscount\b|\boff\b", re.I)
# "这条备注提到折扣了吗"——比 _DISCOUNTISH 略严：不认单独的 "off"，
# 免得 "OFF SITE PICKUP" 这类发货说明被当成折扣依据。
_MEMO_MENTIONS_DISCOUNT = re.compile(r"[%％]|\bdiscounts?\b|\bdisc\b", re.I)
# percent-bearing text that is definitely NOT an order discount:
# - logistics metrics ("95% Fill rate")
# - early-payment / AR terms discounts ("5% Discount may be taken if paid
#   within 20 days", "3% discount ... if payment receive within 15days")
# - freight allowance ("*2% Disc. Freight Allowance")
# Finance handles the latter two after invoicing (Jing 2026-08-17); the
# invoice itself carries no such discount, so the tool never counts them.
_NOT_DISCOUNT = re.compile(
    r"fill\s+rate|paid\s+within|payment\s+receive\w*\s+within|freight\s+allowance"
    # "TERMS: 1% - 360 DAYS SCF" —— 账期付款条件的另一种写法，和 paid within
    # 同类（财务后期处理）。要求带 "N DAYS" 才算，免得把正经折扣误伤掉。
    r"|terms\s*:\s*[0-9.]+\s*[%％]\s*-?\s*\d+\s*days"
    # 一次性折扣：开户折扣、首几单的促销折扣。性质上早就过期，不再计算也不再
    # 提醒（Jing 2026-08-18）。注意只含这两种 —— DropShip / 中国单 / 整托 /
    # 自提那几种每单都可能真的适用，仍然走下面的 _ORDER_TYPE_COND 转人工。
    r"|opening\s+orders?|promotional"
    r"|rebate", re.I)  # rebates: finance handles later (Jing 2026-08-17)
# order-type-dependent discounts (DropShip / China PO / bulk / pickup /
# opening / promotional orders): recognized but never auto-applied — a human
# confirms whether this SO is that order type (Jing 2026-08-17). STK combos
# are the exception, handled by STKCOMBO above.
_ORDER_TYPE_COND = re.compile(
    r"dropship|china\s+order|po\s+ending\s+cn|bulk\s+order|pallet\s+order"
    r"|pickup\s+discount", re.I)
# opening order / promotional originally lived here (Jing 2026-08-17 category C
# = always human review); 2026-08-18 they moved into _NOT_DISCOUNT — one-time
# discounts are expired by nature, so they are ignored outright instead.


# 命中这几种模式即代表整段已被解释完毕：COMBINED / STKCOMBO / SEQ 给出的是全段
# 合计，ORDER-TYPE 本来就整段转人工。其余模式只消费自己匹配到的那一段文字。
WHOLE_SEGMENT_REASONS = {"COMBINED", "STKCOMBO", "SEQ", "ORDER-TYPE"}
_PCT_ANY = re.compile(_PCT)


def _blank(seg, spans):
    """把若干 (start, end) 区间涂成空格，保持其余字符的偏移不变。"""
    chars = list(seg)
    for a, b in spans:
        for i in range(max(a, 0), min(b, len(chars))):
            chars[i] = " "
    return "".join(chars)


def _not_discount_spans(seg):
    """定位"根本不是订单折扣"的从句（履约率 / 账期折扣 / 运费补贴 / 返利）。

    区间会向两侧延伸，把这条从句自己的百分数一起吃掉：只涂掉标志词会留下一个
    孤零零的 "5%"，残留检查就会把一条本来完全理解了的账期折扣报成无法解析。
    延伸遇到逗号/分号即停，同一行里的另一个真折扣因此保得住 —— 这正是原来的
    bug：老代码见到标志词就丢掉整段，
    "5% ... if paid within 20 days, 3% in Lieu of Warranty" 连 3% 一起没了。
    两侧都要看：'2% Disc. Freight Allowance' 的百分数在左边，
    'Fill rate must be at least 93%' 的在右边。
    """
    spans = []
    for m in _NOT_DISCOUNT.finditer(seg):
        start, end = m.start(), m.end()
        if _PCT_ANY.search(m.group(0)):
            # 标志词本身已经把自己那个百分数括进去了（如 "TERMS: 1% - 360 DAYS"），
            # 不能再向外找：一段话里离它最近的百分数可能是隔了半句的正经折扣，
            # 那样会连 "8% LTL order discount" 一起抹掉。
            spans.append((start, end))
            continue
        before = [x for x in _PCT_ANY.finditer(seg) if x.end() <= m.start()]
        if before and not re.search(r"[,;\n]", seg[before[-1].end():m.start()]):
            start = before[-1].start()
        after = [x for x in _PCT_ANY.finditer(seg) if x.start() >= m.end()]
        if after and not re.search(r"[,;\n]", seg[m.end():after[0].start()]):
            end = after[0].end()
        spans.append((start, end))
    return spans


def parse_discount_text(text, so_date):
    """Parse Account Notes / Memo prose into discount candidates.

    Returns (discounts, unparsed_segments).
    Each discount: {rate: Decimal fraction, reason, scope: 'ORDER',
                    threshold_amount: Decimal|None, active: bool}
    Effect/Ending dates on the same segment gate `active` vs so_date.
    """
    discounts, unparsed = [], []
    if not text:
        return discounts, unparsed
    # split on newlines and semicolons: XXX writes one rule per segment
    for seg in re.split(r"[\n;]+", text):
        seg = seg.strip()
        if not seg:
            continue
        # 先把"根本不是订单折扣"的从句涂掉，再看这一段还剩下什么
        masked = _blank(seg, _not_discount_spans(seg))
        if not _PCT_ANY.search(masked):
            continue  # metrics / AR terms / freight / rebates — never counted
        if _ORDER_TYPE_COND.search(masked):
            m = _PCT_ANY.search(masked)
            discounts.append({"rate": Decimal(m.group(1)) / 100, "reason": "ORDER-TYPE",
                              "scope": "ORDER", "threshold_amount": None, "active": True,
                              "conditional": True, "source_text": seg})
            continue
        matched = False
        consumed = []
        for reason, pat in PATTERNS:
            m = pat.search(masked)
            if not m:
                continue
            matched = True
            consumed.append((m.start(), m.end()))
            if reason in ("STKCOMBO", "SEQ"):
                # "7%=(...), THEN 5%" -> sequential compounding
                p1, p2 = Decimal(m.group(1)) / 100, Decimal(m.group(2)) / 100
                rate = Decimal("1") - (Decimal("1") - p1) * (Decimal("1") - p2)
            else:
                rate = Decimal(m.group(1)) / 100
            threshold = D(m.group(2)) if reason == "OVER" else None
            active = True
            eff = _EFFECT.search(seg)
            end = _ENDING.search(seg)
            if eff:
                d = parse_date(eff.group(1))
                if d and so_date and so_date < d:
                    active = False
            if end:
                d = parse_date(end.group(1))
                if d and so_date and so_date > d:
                    active = False
            d = {"rate": rate, "reason": reason, "scope": "ORDER",
                 "threshold_amount": threshold, "active": active,
                 "conditional": reason in CONDITIONAL_REASONS,
                 "source_text": seg}
            if reason == "ITEMSCOPE":
                items = _expand_pns(m.group(3))
                if not items:
                    unparsed.append(seg)
                    continue
                d["scope"] = "ITEMS:" + ",".join(sorted(items))
                d["items"] = items
                d["threshold_qty"] = Decimal(m.group(2))
            discounts.append(d)
            if reason in WHOLE_SEGMENT_REASONS:
                # 只往后吃：约定是"打头的百分比是合计、其后的拆分忽略"。
                # 把匹配位置之前的文字也算作已解释，会吞掉排在合计前面的另一个
                # 折扣 —— 客户 2948 -CAN 的 MEMO 就是这样：
                # 'BUY & SELL ORDER - EXTRA 5% … . 9% Discount= 6% + 3% …'
                # 那 5% 曾被静默丢掉，机器按 9% 对账而 NetSuite 上是 13.55%。
                consumed.append((m.start(), len(masked)))
                break  # stated total covers the rest of the segment
        # Fail-closed 的关键：一段文字必须被解释完整。把已消费的片段涂掉之后，
        # 只要还剩一个百分数，这一整段就交人工 —— 无论前面是否已经认出了别的
        # 折扣。老代码只在"一条模式都没匹配"时才报，于是同段里的第二个折扣
        # 静默消失，而 NetSuite 那边同样漏打时两边"一致"，直接判可开票。
        if _PCT_ANY.search(_blank(masked, consumed)) and _DISCOUNTISH.search(seg):
            unparsed.append(seg)
    return discounts, unparsed


def dedupe_discounts(*discount_lists):
    """Deduplicate on (rate, reason, scope). Memo restating Notes counts once."""
    seen, out = set(), []
    for lst in discount_lists:
        for d in lst:
            key = (str(d["rate"]), d["reason"], d["scope"])
            if key not in seen:
                seen.add(key)
                out.append(d)
    return out


def expected_discount(applicable_base, fixed_amounts, pct_rates, cap=None):
    """Plan §6.4: fixed first, then compound percentages, cap, HALF_UP cents."""
    base = D(applicable_base)
    fixed = sum((D(f) for f in fixed_amounts), Decimal("0"))
    remaining = base - fixed
    combined = Decimal("1")
    for r in pct_rates:
        combined *= (Decimal("1") - Decimal(str(r)))
    combined_rate = Decimal("1") - combined
    pre_cap = fixed + remaining * combined_rate
    if cap is not None:
        pre_cap = min(pre_cap, D(cap))
    return money(pre_cap)


# ── Address comparison (Check 5) ───────────────────────────────────────────

def _norm(s):
    s = re.sub(r"\*+", " ", str(s).upper())
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# EDI writes the country as a code, NetSuite spells it out. Applied only to a
# line that is nothing but the code, so a state abbreviation inside a city line
# ("SAMPLETOWN MI 00000") is never touched.
_COUNTRY_LINE = {"US": "UNITED STATES", "USA": "UNITED STATES",
                 "CA": "CANADA", "CAN": "CANADA", "MX": "MEXICO", "MEX": "MEXICO"}


# What counts as a substantive address line. EDI blocks also carry store/DC
# numbers, country codes and routing codes ("CA******AOD******"), which are not
# address data — and the SO's own country field is unreliable (a Toronto ON
# address is routinely stored with country "United States"), so country is
# never used to judge a mismatch. Only the street line and the
# city/state/postal line have to agree.
_STREET_LINE = re.compile(r"^[0-9]+ +[A-Z]")
_ZIP_US = re.compile(r"(?<![0-9])[0-9]{5}(?:[0-9]{4})?(?![0-9])")
_ZIP_CA = re.compile(r"(?<![A-Z0-9])[A-Z][0-9][A-Z] ?[0-9][A-Z][0-9](?![A-Z0-9])")


def _is_substantive(line):
    # a digits-only line is the EDI store/DC number, not a postal code
    if line.replace(" ", "").isdigit():
        return False
    return bool(_STREET_LINE.match(line) or _ZIP_US.search(line) or _ZIP_CA.search(line))


# 同一个地址两边写法不同是常态，逐字包含匹配会把相同的地址判成不一致：
#   EDI "100 Main East Street"        SO "100 RUE Main Street East"  词序 + 多一个 RUE
#   EDI "200 Boul. des SampleNord" SO "200 Boul. des Sample-Nord"  连写
# 所以按词比对：EDI 那一行的每个词都要能在 SO 地址里找到，词序不论。
# 方向词（East/Est）是有区分度的，必须对上；街道类型词（Street/Rue/Road…）
# 只是称谓，两边用词或语言不同不算不一致。
_DIRECTION = {"EST": "EAST", "E": "EAST", "OUEST": "WEST", "O": "WEST", "W": "WEST",
              "NORD": "NORTH", "N": "NORTH", "SUD": "SOUTH", "S": "SOUTH"}
_STREET_TYPE = {"STREET", "ST", "RUE", "ROAD", "RD", "AVENUE", "AVE", "AV",
                "BOULEVARD", "BOUL", "BLVD", "DRIVE", "DR", "COURT", "CT",
                "LANE", "LN", "WAY", "PLACE", "PL", "HIGHWAY", "HWY", "DES", "DE"}


def _addr_tokens(norm_text):
    return [_DIRECTION.get(t, t) for t in norm_text.split()]


def _so_token_index(so_norm):
    """SO 地址的词表，外加相邻 2~3 个词的连写形式。

    连写是为了让 EDI 的 "SAMPLENORD" 能对上 SO 的 "Sample-Nord"
    （规范化后是两个词）。
    """
    toks = _addr_tokens(so_norm)
    idx = set(toks)
    for n in (2, 3):
        for i in range(len(toks) - n + 1):
            idx.add("".join(toks[i:i + n]))
    return idx


def _line_missing_tokens(line_norm, so_norm):
    """EDI 这一行里、在 SO 地址中找不到的词。空列表表示这一行对得上。"""
    idx = _so_token_index(so_norm)
    toks = _addr_tokens(line_norm)
    missing, i = [], 0
    while i < len(toks):
        for take in (3, 2, 1):          # EDI 拆开写、SO 连写的情况
            if "".join(toks[i:i + take]) in idx:
                i += take
                break
        else:
            if toks[i] not in _STREET_TYPE:
                missing.append(toks[i])
            i += 1
    return missing


def compare_edi_address(edi_lines, so_address_text):
    """Compare the EDI ship-to block against the SO shipping address.

    Returns (status, details). Asterisks are XXX placeholder padding and are
    stripped. Only substantive lines (street, city/state/postal) must appear in
    the SO address; store numbers, country codes and routing codes are reported
    but never cause a mismatch. PASS requires at least one substantive line —
    if none survives, the evidence is too thin to clear and it is NEEDS-REVIEW.

    Matching is per word, not literal text: the same address is routinely
    written differently on the two sides (see _line_missing_tokens).
    """
    so_norm = _norm(so_address_text)
    lines = [ln for ln in (_norm(l) for l in edi_lines) if ln]
    compare = [ln for ln in lines if _is_substantive(ln)]
    ignored = [ln for ln in lines if not _is_substantive(ln)]
    notes = [f"未参与比对（门店号/国家码/路由码等非地址内容）：'{ln}'" for ln in ignored]
    if not compare:
        return "NEEDS-REVIEW", notes + ["EDI 地址块里没有可比对的街道/城市邮编行 — 需人工核对"]
    mismatches = []
    for ln in compare:
        missing = _line_missing_tokens(ln, so_norm)
        if missing:
            mismatches.append(
                f"EDI 地址行在 SO 收货地址中找不到，不一致：'{ln}'（对不上的词：{'、'.join(missing)}）")
    if mismatches:
        return "FAIL", notes + mismatches
    return "PASS", notes + [f"已匹配：'{ln}'" for ln in compare]


# ── Checks ─────────────────────────────────────────────────────────────────

def check_eligibility(so, rules):
    ev = [f"SO 状态：{so['status']}"]
    allowed = rules["ALLOWED_SO_STATUSES"]
    fulfills = so.get("fulfillments", [])
    invoices = so.get("invoices", [])
    for f in fulfills:
        ev.append(f"发货单 {f['number']}（{f.get('date','?')}）：{f['status']}")
    for i in invoices:
        ev.append(f"发票 {i['number']}（{i.get('date','?')}）：{i.get('status','')}")
    if invoices:
        return "FAIL", ev + ["已开票：该 SO 下已存在发票，XXX 规定一张 SO 只开一张发票 — 禁止重复开票"]
    if so["status"] not in allowed:
        return "FAIL", ev + [f"SO 状态不在允许开票的状态列表内 {allowed} — 禁止开票"]
    if not fulfills:
        return "FAIL", ev + ["缺失：该 SO 没有任何发货单（Item Fulfillment）— 禁止开票"]
    bad = [f for f in fulfills if f["status"] != "Shipped"]
    if bad:
        return "FAIL", ev + [f"存在未发货（非 Shipped）的发货单：{[f['number'] for f in bad]} — 禁止开票"]
    return "PASS", ev


# Special Discounts sheet column order (see references/workbook-schema.md)
SPECIAL_SO, SPECIAL_PO, SPECIAL_CUST, SPECIAL_ACTIVE = 1, 2, 3, 14
# Seasonal Discounts sheet
SEASONAL_CUST, SEASONAL_FROM, SEASONAL_TO, SEASONAL_ACTIVE = 1, 5, 6, 11


def _is_true(v):
    return str(v).strip().upper() in ("TRUE", "T", "1", "YES", "Y")


def _matching_special_rows(rows, so, customer):
    """Active Special Discounts rows that actually apply to THIS SO.

    Matching on SO / PO / customer matters: the old code only asked whether the
    sheet had any row at all, so one unrelated entry disabled the
    "memo-only discount has no backing evidence" guard for every customer.
    """
    out = []
    for r in rows or []:
        r = list(r) + [""] * 16
        if not _is_true(r[SPECIAL_ACTIVE]):
            continue
        keys = (str(r[SPECIAL_SO] or "").strip(), str(r[SPECIAL_PO] or "").strip(),
                str(r[SPECIAL_CUST] or "").strip())
        if (keys[0] and keys[0] == str(so.get("so_number", "")).strip()) or \
           (keys[1] and keys[1] == str(so.get("po_number", "")).strip()) or \
           (keys[2] and keys[2] == str(customer.get("internal_id", "")).strip()):
            out.append(r)
    return out


def _matching_seasonal_rows(rows, so, customer, so_date):
    """Active Seasonal Discounts rows effective for this customer on the SO date.

    The engine does not yet compute seasonal discounts. Rather than ignore a
    configured campaign (which would silently judge the SO against Account
    Notes alone), any match is surfaced so the check fails closed.
    """
    out = []
    for r in rows or []:
        r = list(r) + [""] * 12
        if not _is_true(r[SEASONAL_ACTIVE]):
            continue
        if str(r[SEASONAL_CUST] or "").strip() != str(customer.get("internal_id", "")).strip():
            continue
        start, end = parse_date(r[SEASONAL_FROM]), parse_date(r[SEASONAL_TO])
        if so_date and start and so_date < start:
            continue
        if so_date and end and so_date > end:
            continue
        out.append(r)
    return out


def _row_to_discount(r, kind, value_col, scope_col, qty_col, cap_col, type_col, reason_col):
    """Turn a workbook discount row into the same shape the text parser emits."""
    raw_type = str(r[type_col] or "").strip().upper()
    try:
        val = D(r[value_col])
    except Exception:
        return None
    if raw_type == "AMOUNT":
        rate, fixed = Decimal("0"), val
    else:
        # PERCENT values are stored as fractions (0.03 = 3%)
        rate, fixed = val, Decimal("0")
    d = {"rate": rate, "fixed": fixed, "reason": f"{kind}:{str(r[reason_col] or '').strip()}",
         "scope": "ORDER", "threshold_amount": None, "active": True, "conditional": False,
         "source_text": f"{kind} 表 {str(r[0] or '').strip()}", "from_workbook": True,
         "cap": None}
    cap_raw = str(r[cap_col] or "").strip()
    if cap_raw:
        try:
            d["cap"] = D(cap_raw)
        except Exception:
            d["cap"] = None
    scope = str(r[scope_col] or "").strip()
    if scope:
        d["items"] = [scope]
        d["scope"] = "ITEMS:" + scope
        qty = str(r[qty_col] or "").strip()
        d["threshold_qty"] = D(qty) if qty else Decimal("0")
    return d


def check_discount(so, customer, workbook, rules):
    so_date = parse_date(so.get("date"))
    notes_disc, notes_unparsed = parse_discount_text(customer.get("account_notes", ""), so_date)
    memo_disc, memo_unparsed = parse_discount_text(so.get("memo", ""), so_date)
    ev, review = [], []

    active_notes = [d for d in notes_disc if d["active"]]
    active_memo = [d for d in memo_disc if d["active"]]
    merged = dedupe_discounts(active_notes, active_memo)

    # Memo-only discount not backed by Notes/Excel -> needs review (plan §6.2)
    workbook = workbook or {}
    notes_keys = {(str(d["rate"]), d["reason"]) for d in active_notes}
    special_rows = _matching_special_rows(workbook.get("special_rows"), so, customer)
    for d in active_memo:
        if (str(d["rate"]), d["reason"]) not in notes_keys and not special_rows:
            review.append(f"折扣只出现在 SO 备注里、客户资料和折扣表中都没有依据，需人工确认：'{d['source_text']}'")

    # Workbook rules join the same calculation as the text-parsed ones and
    # compound with them (Jing 2026-08-18).
    workbook_discounts = []
    for r in _matching_seasonal_rows(workbook.get("seasonal_rows"), so, customer, so_date):
        d = _row_to_discount(r, "季节性折扣", 4, 7, 8, 9, 3, 2)
        if d:
            workbook_discounts.append(d)
            ev.append(f"季节性折扣规则 {r[0]}（{r[2]}）生效")
    for r in special_rows:
        d = _row_to_discount(r, "特批折扣", 6, 7, 8, 9, 5, 4)
        if d:
            workbook_discounts.append(d)
            ev.append(f"特批折扣记录 {r[0]}（{r[4]}）匹配本 SO")
    merged = merged + workbook_discounts

    for seg in notes_unparsed + memo_unparsed:
        review.append(f"无法解析的折扣文字，需人工确认：'{seg}'")

    # order-subtotal thresholds ("over $3,000") and item-scope eligibility
    subtotal = D(so["subtotal"])
    lines = so.get("lines", [])
    order_rates, item_raw, fixed_total = [], Decimal("0"), Decimal("0")
    capped = []
    for d in merged:
        if d.get("cap"):
            # A cap changes the arithmetic and Jing wants a human on it.
            capped.append(d)
            review.append(f"折扣规则『{d['source_text']}』设置了封顶金额 {d['cap']} — "
                          f"工具不自动套用封顶，需人工核对最终折扣")
            continue
        if d.get("conditional"):
            why = _CONDITIONAL_REVIEW_TEXT.get(d["reason"], _CONDITIONAL_REVIEW_DEFAULT)
            review.append(f"{why}：'{d['source_text'][:80]}'")
            continue
        if d["threshold_amount"] is not None and subtotal < d["threshold_amount"]:
            ev.append(f"{d['rate']:%}『{d['reason']}』未达门槛，不计入：小计 {subtotal} < 门槛 {d['threshold_amount']}")
            continue
        if d.get("items"):
            # Jing's rule: listed PNs together or individually reaching the
            # threshold (on ORDERED qty) earn the discount on those PNs only,
            # calculated on their SHIPPED value.
            scoped = [l for l in lines if str(l.get("item", "")).strip() in d["items"]]
            if not scoped:
                # Guard against silent item-code format mismatch: if nothing
                # matched exactly but a line looks like one of the listed PNs
                # (same leading digits), a human must confirm rather than the
                # tool concluding "no discount due".
                bases = {re.sub(r"[LR]$", "", i) for i in d["items"]}
                near = [l["item"] for l in lines
                        if any(str(l.get("item", "")).strip().startswith(b) for b in bases)]
                if near:
                    review.append(
                        f"按件号折扣规则指定 {d['items']}，但 SO 上只有相近的件号 {near}，"
                        f"未精确匹配 — 需人工确认件号对应关系后再开票")
                    continue
            ordered = sum(D(l.get("qty", 0)) for l in scoped)
            if ordered < d["threshold_qty"]:
                ev.append(f"按件号折扣未达数量门槛，不计入：{d['items']} 下单数 {ordered} "
                          f"< {d['threshold_qty']}（匹配到 {len(scoped)} 行）")
                continue
            shipped_base = sum(D(l.get("qty_fulfilled", l.get("qty", 0))) * D(l.get("rate", 0))
                               for l in scoped)
            item_raw += shipped_base * d["rate"]
            ev.append(f"按件号折扣：{float(d['rate'])*100:g}% 用于 {d['items']}"
                      f"（下单数 {ordered} ≥ {d['threshold_qty']}，已发货基数 {money(shipped_base)}）")
            continue
        if d.get("fixed"):
            fixed_total += D(d["fixed"])
            ev.append(f"适用固定金额折扣：{money(d['fixed'])}（{d['reason']}）")
            continue
        order_rates.append(d["rate"])
        ev.append(f"适用折扣：{float(d['rate'])*100:g}%（{d['reason']}），依据：'{d['source_text'][:60]}'")

    # Plan §6.4: fixed amounts come off first, percentages compound on the rest.
    combined = Decimal("1")
    for r in order_rates:
        combined *= (Decimal("1") - Decimal(str(r)))
    combined_rate = Decimal("1") - combined
    expected = money(fixed_total + (subtotal - fixed_total) * combined_rate + item_raw)
    if len(order_rates) > 1:
        ev.append(f"多个折扣相乘：{[f'{float(r)*100:g}%' for r in order_rates]} "
                  f"→ 合计 {float(combined_rate)*100:g}%")
    actual = money(abs(D(so.get("discount_amount", 0))))
    ev.append(f"应折 {expected} vs 实折 {actual}（折扣项：{so.get('discount_item_name') or '—'}）")

    # Jing 2026-08-18：SO 上有折扣，就一定要有对应的备注说明。备注是这笔折扣
    # 在单据上唯一的书面依据；金额可能完全正确，但没写依据就交人工补。
    # 判定看备注里有没有提到折扣（百分比或 disc/discount 字样），只是非空不算 ——
    # 通篇发货说明的备注解释不了折扣。
    if actual > 0 and not _MEMO_MENTIONS_DISCOUNT.search(str(so.get("memo") or "")):
        review.append(f"SO 上有折扣 {actual}，但 SO 备注里没有提到折扣 — "
                      f"需人工确认这笔折扣的依据，并把说明补进备注")

    # summary equation
    total_calc = money(D(so["subtotal"]) - abs(D(so.get("discount_amount", 0)))
                       + D(so.get("shipping_cost", 0)) + D(so.get("misc_cost", 0)))
    if total_calc != money(so["total"]):
        return "FAIL", ev + [f"金额勾稽不一致：小计−折扣+运费+杂费 = {total_calc}，"
                             f"与 SO 合计 {money(so['total'])} 对不上"], review

    # Jing 2026-08-18: several discounts on one SO are computed automatically,
    # but a human still signs off — compounding is where a wrong reading costs
    # the most, so a clean match is reported as "matches, please confirm".
    applied_count = len(order_rates) + (1 if fixed_total else 0) + (1 if item_raw else 0)
    if applied_count > 1:
        review.append(f"本 SO 命中 {applied_count} 项折扣并按相乘计算（合计 "
                      f"{float(combined_rate)*100:g}%）— 金额虽已核对一致，仍需人工确认")

    tol = D(rules.get("DISCOUNT_AMOUNT_TOLERANCE_USD", 0))
    if abs(expected - actual) > tol:
        note = f"折扣金额差异 {abs(expected - actual)}（超出容差 {tol}）"
        if review:
            # unresolved conditional/unparsed discounts could explain the
            # difference — a human must decide, but never auto-release
            return "NEEDS-REVIEW", ev + [note], review
        return "FAIL", ev + [note], review
    if review:
        return "NEEDS-REVIEW", ev, review
    return "PASS", ev, review


def check_price_level(so, customer):
    lines = so.get("lines", [])
    if not lines:
        # Falling through would compare an empty set against the customer
        # default and report a price-level mismatch, sending the operator to
        # look for a discrepancy that does not exist.
        return "NEEDS-REVIEW", ["SO 上没有可检查的库存商品行（可能全为套件/组装件/非库存项，"
                                "或行读取失败）— 价格等级与单价检查无法覆盖，需人工核对"]
    # Prefer internal-id comparison (immune to display renames); fall back to
    # display names when ids are unavailable (e.g. unit tests / REST source).
    use_ids = customer.get("price_level_id") and all(l.get("price_level_id") for l in lines)
    key = "price_level_id" if use_ids else "price_level"
    default_pl = str(customer.get(key, "")).strip()
    pls = {str(l.get(key, "")).strip() for l in lines}
    disp = sorted({str(l.get("price_level", "")).strip() for l in lines})
    ev = [f"客户默认价格等级：{str(customer.get('price_level','')).strip() or '（空白）'}"
          + (f"（内部 id {customer.get('price_level_id')}）" if use_ids else ""),
          f"SO 行上出现的价格等级：{disp}" + (f"（ids {sorted(pls)}）" if use_ids else "")]
    if not default_pl:
        return "NEEDS-REVIEW", ev + ["客户默认价格等级为空白或读取不到 — 需人工确认"]
    if "" in pls:
        blanks = [l["line_id"] for l in lines if not str(l.get(key, "")).strip()]
        return "FAIL", ev + [f"以下行缺失价格等级：{blanks} — 禁止开票"]
    if len(pls) > 1:
        return "FAIL", ev + ["SO 行的价格等级不一致（多种混用）— 需人工判断是 SO 录错还是客户设置错"]
    if pls != {default_pl}:
        return "FAIL", ev + ["SO 价格等级与客户默认价格等级不符 — 需人工判断哪一边是错的"]
    return "PASS", ev


def check_unit_price(so, rules):
    max_rate = D(rules.get("MAX_ITEM_RATE_USD", 1000))
    bad = []
    for l in so.get("lines", []):
        rate = D(l["rate"])
        pl_num = re.sub(r"\D", "", str(l.get("price_level", "")))
        if rate > max_rate:
            bad.append(f"第 {l['line_id']} 行 物料 {l['item']}：单价 {rate} 超过上限 {max_rate} — 需人工核对")
        if pl_num and rate == D(pl_num):
            bad.append(f"第 {l['line_id']} 行 物料 {l['item']}：单价 {rate} 与价格等级编号 {pl_num} 相同 — 疑似录入错误，需人工核对")
    if bad:
        return "FAIL", bad
    return "PASS", [f"全部 {len(so.get('lines', []))} 行单价均 ≤ {max_rate}，且没有与价格等级编号相同的"]


def check_fulfillment(so, customer, rules=None):
    fulfills = so.get("fulfillments", [])
    ev = [f"{f['number']}：{f['status']}" for f in fulfills]
    if not fulfills:
        return "FAIL", ["缺失：没有发货单（Item Fulfillment）— 禁止开票"]
    bad = [f for f in fulfills if f["status"] != "Shipped"]
    if bad:
        return "FAIL", ev + ["存在未发货（非 Shipped）的发货单 — 禁止开票"]
    if customer.get("overseas") and len(fulfills) > 1:
        return check_truck_scope(so, fulfills, ev, rules or {})
    return "PASS", ev


def check_truck_scope(so, fulfills, ev, rules):
    """海外单只要还装得下一整车就自动放行，超了才由人来挑装车范围。

    Jing 2026-08-18 的口径：整单材料重量 ≤ 40,000 lb **且**发货单张数 ≤ 26。
    （8/17 最初定的是托盘/包裹数 ≤ 26，8/18 改为发货单张数；托盘数现在只作为
    参考信息写进证据，不参与判断。）

    失败关闭：整单重量读不到就 NEEDS-REVIEW —— 重量是唯一还会拦人的数字。
    """
    max_w = D(rules.get("MAX_TRUCK_WEIGHT_LB", 40000))
    # 2026-08-18 Jing 改口径：26 数的是**发货单张数**，不是托盘/包裹数。
    # 工作簿里的键已同步改名为 MAX_TRUCK_FULFILLMENTS；旧名 MAX_TRUCK_PACKAGES
    # 仍作回落，只为兼容手上可能还留着的旧工作簿副本。
    max_n = D(rules.get("MAX_TRUCK_FULFILLMENTS", rules.get("MAX_TRUCK_PACKAGES", 26)))
    count = len(fulfills)
    head = f"海外客户，共 {count} 张发货单（上限 {max_n}）"

    raw_w = str(so.get("items_total_weight") or "").strip()
    try:
        weight = D(raw_w) if raw_w else None
    except Exception:
        weight = None

    if weight is None:
        return "NEEDS-REVIEW", ev + [
            f"{head}；整单重量（SO 上的 items total weight）读不到 — "
            f"需人工确认本次开票的装车范围"]

    detail = f"{head}；整单重量 {weight} lb（上限 {max_w}）"
    # 托盘/包裹数只作参考写进证据，不参与判断，读不到也不再拦人
    known = [int(f["package_count"]) for f in fulfills if f.get("package_count") is not None]
    if known:
        detail += f"；托盘/包裹合计 {sum(known)} 个（仅供参考，不参与判断）"

    over = []
    if weight > max_w:
        over.append(f"重量超限（{weight} > {max_w} lb）")
    if D(count) > max_n:
        over.append(f"发货单数超限（{count} > {max_n}）")
    if over:
        return "NEEDS-REVIEW", ev + [
            f"{detail}；{'、'.join(over)}，超出一整车 — 需人工确认本次开票覆盖哪几张发货单（装车范围）"]
    return "PASS", ev + [detail + "；在一整车范围内，自动放行"]


def check_edi(so):
    edi = so.get("edi_ship_lines")
    if not edi:
        if str(so.get("edi_info_text") or "").strip():
            # EDI INFO carries prose only (no '*' address block, no
            # ShippingDetails) — say so instead of silently claiming "non-EDI"
            return "N/A", ["EDI INFO 只有说明文字、没有地址块（无 * 开头的地址行），不做地址比对"]
        return "N/A", ["非 EDI 订单（SO 上没有 EDI INFO）"]
    status, details = compare_edi_address(edi, so.get("ship_address_text", ""))
    if status != "FAIL":
        return status, details
    # A very common XXX shape: the EDI block holds the bill-to instead of the
    # ship-to. That is not obviously an error, but it must never pass silently.
    bill = so.get("bill_address_text", "")
    if bill and compare_edi_address(edi, bill)[0] == "PASS":
        return "NEEDS-REVIEW", details + [
            "EDI 地址与 SO 收货地址不符，但与账单地址完全一致 — "
            "需人工确认这个 EDI 客户的 EDI INFO 放的是收货地址还是账单地址"]
    return status, details


OVERSEAS_SUFFIX = re.compile(r"-\s*[A-Za-z]")


def run_checks(so, customer, workbook, rules):
    """so/customer: normalized dicts. Returns full report dict."""
    results = {}
    results["0-eligibility"] = check_eligibility(so, rules)
    d_status, d_ev, d_review = check_discount(so, customer, workbook, rules)
    results["1-discount"] = (d_status, d_ev + d_review)
    results["2-price-level"] = check_price_level(so, customer)
    results["3-unit-price"] = check_unit_price(so, rules)
    results["4-fulfillment"] = check_fulfillment(so, customer, rules)
    results["5-edi"] = check_edi(so)

    statuses = [s for s, _ in results.values()]
    if "FAIL" in statuses:
        overall = "DO-NOT-INVOICE"
    elif "NEEDS-REVIEW" in statuses:
        overall = "NEEDS-REVIEW"
    else:
        overall = "INVOICE-READY"
    return {"so_number": so["so_number"], "overall": overall,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "customer": {"code": customer.get("code"), "name": customer.get("name"),
                         "internal_id": customer.get("internal_id"),
                         "handling": customer.get("handling", "STANDARD")},
            "so_date": so.get("date"), "so_status": so.get("status"),
            "checks": {k: {"status": s, "evidence": e} for k, (s, e) in results.items()}}
