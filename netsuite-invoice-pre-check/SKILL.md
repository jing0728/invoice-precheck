---
name: netsuite-invoice-pre-check
description: XXX 开票预检（NetSuite Sales Order invoice-readiness pre-check）。Use this skill whenever the user asks to 检查 SO / check SO, 跑开票预检 / run an invoice pre-check, 看今天的待开票队列 / scan the billing queue or Team 3 tab of the XXX Order Workflow app, 手动跑一次 3 点的扫描 / run the daily 3pm billing scan, 回填发票号 / back-fill invoice numbers and amounts, 或只是贴几个 SO 号说「查一下这几单」。首选做法是驱动本地工具 invoice-precheck-tool（localhost:3100），而不是自己用浏览器一项项查。
---

# XXX 开票预检 (v2 · 2026-08-18)

判断 NetSuite Sales Order 能不能开票，产出四种结论之一：
`INVOICE-READY` / `DO-NOT-INVOICE` / `NEEDS-REVIEW` / `CHECK-INCOMPLETE`。

**规则的真理源是代码，不是这份文件。** 五项检查的实现在
`invoice-precheck-tool/engine.py`，每条口径都有对应的单元测试
（`tests/test_engine.py` 68 项 + `tests/test_app.py` 5 项）。本文件只负责：
怎么驱动工具、当前口径是什么、工具挂了怎么人工顶上。**任何阈值、比例、
状态白名单都不要在这里复述具体数值** —— 那些读工作簿的 `Rules` 表。

## 首选路径：驱动本地工具

工具在 `C:\Users\<user>\Desktop\invoice审查系统\invoice-precheck-tool\`，
开机自启，页面 http://localhost:3100/ 。**只监听 127.0.0.1**（接口无鉴权，
且会写 NetSuite 和开票系统，不要改成对局域网开放）。

| 要做的事 | 怎么做 |
|---|---|
| 查几个指定的 SO | `POST /api/scan {"so_numbers": "SO9000004, 1398700"}` —— **只读**，不写 NetSuite、不回填 |
| 跑一遍今天的待开票队列 | `POST /api/run` —— 队列模式，**会写**（见下节） |
| 看最近一次结果 | `GET /api/report/latest`；历史报告 `GET /api/reports` + `/api/report/file/<name>` |
| 看环境是否就绪 | `GET /api/preflight`（约 3 分钟才返回，正常） |
| 看队列里现在有什么 | `GET /api/queue` |
| 人工改判 | `POST /api/override {so_number, overall, by, note, was, report_file}` |
| 已回填过的单 | `GET /api/backfilled` |

同一时刻只允许一次扫描，第二次会拿到 **409**（并发扫描会让两个回填抢同一个
浏览器标签页）。拿到 409 就等，不要重试绕过。

报告落在 `invoice-precheck-tool/reports/`，`latest.json` 是最近一次。

### 跑之前先确认前提

1. 调试 Chrome 开着且端口 9222 有响应（与补货/照片系统同一个 Chrome）。
2. Chrome 里 NetSuite 已登录。会话过期时所有 SO 记 `CHECK-INCOMPLETE` ——
   **这是正确行为，不是故障**；让用户去那个 Chrome 窗口登录，不要代劳，
   更不要碰 MFA。
3. 回填还需要 billing app 标签页已登录（登录存在 sessionStorage，是按标签页
   的，Chrome 重启后只有恢复了那个标签页才在）。检查本身不受影响。
4. 参考工作簿四张表都是 `ACTIVE`。加载失败时整批记 `CHECK-INCOMPLETE`
   且**不产生任何写入**。

`GET /api/preflight` 会把上面这些一次性告诉你。

## 工具会写什么（务必清楚）

锁定方案原本写的是"NetSuite 全程只读"。**这条已被 Jing 于 2026-08-17 明确
推翻**，现状是：

1. **NetSuite —— 只改 FORM 一个字段**，211「XXX - Sales Order with Rate」
   → 219「…with Rate with Close」。仅队列扫描会写；仅对**检查真正跑完**的
   SO（结论为 INVOICE-READY / NEEDS-REVIEW / DO-NOT-INVOICE）；
   `CHECK-INCOMPLETE` 和已开票的 SO 绝不写。做法是用 `cf=219` 参数把记录
   直接加载成目标表单再保存，不往任何字段里打字；保存前后对整条记录 XML
   做全字段差异比对，除白名单元数据外任何变动都会被标出来交人工；每次写入
   审计到 `reports/form_changes/`。
2. **开票系统 —— 自动回填**。队列扫描发现"NetSuite 已开票、app 仍 Unbilled"
   的 SO，自动读发票号/金额写回。发票号和金额是事实、不含判断，所以不需要
   逐单确认（Jing 2026-08-17）。走 app 自己的 `markBilled`，但**抑制逐条
   保存**、最后只提交一次并等返回，再用独立的 `GET /db` 核对，只有服务器
   确认的行才算成功。**永远不要拿浏览器内存里的状态当写入成功的证据**，也
   永远不要直接 `POST /db`（那会整包上传全部订单）。
3. **手工扫描（`/api/scan`）什么都不写。**

## 当前口径（与锁定方案不同的部分）

方案 `PLAN.md` 是 2026-08-13 锁定的，下面这些是之后由 Jing 拍板改掉的。
括号里是决定日期。

- **折扣项不限**：`Custom Discount` 或任何预设折扣项（如「8% Discount」）都
  可以，只看金额是否精确到分（8/17）。
- **海外多发货单不再一律人工**：整车规则 —— 整单材料重量 ≤ 40,000 lb **且**
  发货单张数 ≤ 26 就自动放行，任一超限才交人工选装车范围。重量取 SO 上的
  整单材料重量，读不到就失败关闭（这是唯一还会拦人的数字）。阈值在 `Rules`
  表：`MAX_TRUCK_WEIGHT_LB` 与 `MAX_TRUCK_FULFILLMENTS`。
  8/17 最初定的是托盘/包裹数 ≤ 26，**8/18 改为发货单张数**；托盘数现在只作
  参考写进证据，不参与判断。
- **EDI 地址三档**：星号是 XXX 的占位符约定；只比对"实质地址行"（街道行、
  城市/州/邮编行），门店号、国家码、路由码报告但不参与比对，SO 自己的国家
  字段不可靠（多伦多的地址常被存成 United States）所以国家永不作为不一致
  依据。EDI 块与**账单地址**完全一致但与收货地址不符 → 单独一档
  `NEEDS-REVIEW`，不是 FAIL 也不是静默通过（8/17）。
- **折扣文本的分类裁定**（8/17，来自 561 个客户账户备注的全量盘点）：保修
  类各种写法一律自动按标称比例计；组合写法（`10%=3%+7%COD`）取**打头的
  合计**；订单类型类（DropShip / 中国单 / 整托 / 自提）一律交人工，因为每单
  都可能真的适用；各类 Rebate、运费补贴、账期/早付折扣（含
  `TERMS: 1% - 360 DAYS` 这种写法）、履约率一律**不是开票折扣**，财务后期
  处理。
- **一次性折扣不再计算也不再提醒**（8/18）：开户折扣（opening order）、首几单
  的促销折扣（promotional）性质上早就过期了，归入"不是开票折扣"那一类。
  8/17 它们原本和 DropShip 一起属于"一律交人工"。
- **有折扣就必须有对应备注**（8/18）：SO 上的折扣金额 > 0，而 SO 备注里既没有
  百分比也没有 disc/discount 字样 → NEEDS-REVIEW。备注是这笔折扣在单据上唯一
  的书面依据；金额可能完全正确，但没写依据就交人工补。只是备注非空不算 ——
  通篇发货说明解释不了折扣。（实测 23 张队列：11 张有折扣，11 张都写了备注，
  新增人工 0 张。）
- **封顶不自动套用**：带 Cap 的规则一律交人工核最终折扣。
- **一单命中多项折扣时**：工具照样按相乘算出金额并核对，但仍然交人工确认
  —— 复合是读错代价最大的地方（8/18）。
- **`先 X% 再 Y%` 式的连续折扣**：认得出来但绝不自动计算，一律交人工。这种
  写法目前只出现在 3011 -CAN / 3000 / 2973 -CSAM 上，而这几个不是 XXX 这边
  的账号（Jing 8/18）。唯一例外是已用 NetSuite 截图确认过口径的
  `*STK Orders: 7%=(...), THEN 5%COD`（客户 2936 -MEX，8/17）。
- **人工改判只对当次那批扫描有效**。更晚的扫描从头重判，旧改判降级为提示。
  只有"已回填 INVOICED"是终态。机器结论永不被改写，改判追加在旁边，审计
  时两者都看得到（8/17）。
- **允许开票的状态**多了组合状态「Pending Billing/Partially Fulfilled」，
  在 `Rules` 表里维护（8/17）。

## 降级路径：工具不可用时的人工流程

只有在工具确实起不来、而用户又必须马上判一单时才走这条。逐单处理，一单
读不出来不影响其余单。

**Step 0 定位**：全局搜 SO 号打开记录，并用 `tranid` 严格相等证明拿到的就是
这一单 —— **绝不模糊匹配**，证不出来就 `CHECK-INCOMPLETE`。记录 SO 内部 ID、
检查时刻（带时区）、SO 日期、状态、客户、PO、备注、小计、折扣项与金额、
运费杂费、合计、收货地址、有没有 EDI INFO。商品行很长时要拉到底并核对行数，
抓不全不算通过。经典 UI 加 `&xml=T` 可以直接拿到结构化 XML，比翻 DOM 可靠。

**Step 1 资格**：状态必须在白名单内；必须至少有一张发货单且**每一张**都是
`Shipped`（有 Picked/Packed 就是禁止开票）；未发完的剩余数量不阻塞开票；
Related Records 里已经有发票 → 已开票，禁止重复开。把所有发货单和发票的
号码、日期、状态都列出来。

**Step 2 客户**：从 SO 上的客户链接进客户记录取**内部 ID**（不要拿 SO URL
里的 `id`，那是 SO 自己的）。用内部 ID 去 `Customers` 表匹配，**再按 SO 日期
挑出生效的那一行**（一个客户可以有多行不同生效期；空的起止表示该端不限）。
表里没有、没有在 SO 日期生效的行、多行同时生效、或被标记为不活跃 → 禁止
开票。读客户的默认价格等级（Financial > Account
Information），比对用内部 id 不用显示名。海外标记由显示名的 `-后缀` 推导，
再被工作簿的 `CAN Rule` 覆盖。`Handling Type`（STANDARD/COD/COD-PR）只是
报告标记，v1 不触发任何动作。

**五项检查**：
1. **折扣**：证据来自客户账户备注、SO 备注、`Seasonal Discounts` 生效行、
   `Special Discounts` 匹配行。邮件证据链尚未接入，凡是需要邮件佐证的一律
   交人工。去重按「比例 + 事由 + 适用范围」，SO 备注重复账户备注只算一次。
   门槛判定用**下单数**（「over 64」= 64 及以上，同一料号多行要合并），
   算钱用**已发货数**。算法见 `references/discount-calculation.md`。与
   NetSuite 折扣金额比对**精确到分**，并验证小计−折扣+运费+杂费=合计。
   解析不出来的文字一律交人工，绝不猜。
2. **价格等级**：只看正常商品行；每行都要有价格等级、必须同一个、且等于
   客户默认值。客户默认值空白或读不到 → 人工；确实不一致 → 禁止开票并说明
   需要人判断是 SO 录错还是客户设置错。
3. **单价异常**：先解析货币符号和千分位再比数值。单价超过上限 → 拦；单价
   数值与价格等级编号相同（如单价 809.00、价格等级「809」）→ 拦，疑似录入
   错误。列出行号/料号/价格等级/单价。
4. **发货状态**：列出全部发货单与状态；海外客户多发货单走上面的整车规则。
5. **EDI 地址**：没有 EDI INFO → `N/A`，不阻塞。有则按上面的三档口径比。

**结论优先级**：读不全 → `CHECK-INCOMPLETE`；任何一项 FAIL → `DO-NOT-INVOICE`；
无 FAIL 但有需人工 → `NEEDS-REVIEW`；全部通过或确实不适用 → `INVOICE-READY`。
`N/A` 永远不能用来掩盖缺失的证据。

**Fail-closed 是硬规则**：缺失、冲突、读不出、未验证的证据，一律不允许产出
`INVOICE-READY`。拿不准就 `NEEDS-REVIEW`。

报告格式见 `references/report-contract.md`（人工路径用）；工具路径直接给
JSON 和页面，不需要再套模板。

## 绝不做

- 在 NetSuite 里创建/提交/修改/删除发票或 SO —— **唯一允许的写入是上面
  那个 FORM 字段切换**，除此之外一个字段都不碰。
- 替用户登录、处理 MFA、代填任何凭据。
- 直接 `POST /db` 写开票系统（只能走 app 自己的 UI 路径）。
- 替用户决定海外多车订单开哪几车、给一张 SO 开第二张发票、生成 COD-PR
  Excel 发票、发邮件、解释 Customer Group Pricing / Item Pricing。
- 在检查过程中修改参考工作簿。

## 改口径的顺序

发现规则要变时，**先改测试，再改代码，最后回来更新本文件**。反过来做就是
这份文件之前停在 v1.1、而工具已经跑到 v1.4 的原因 —— 那时它还写着
"NetSuite 全程只读"和"海外多发货单一律人工"，两条都已经不成立了。

代码改完记得重启工具服务（页面 HTML 改动不用重启，Python 必须重启）。
