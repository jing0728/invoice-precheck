# Invoice Pre-Check Tool（独立本地网页工具，无 LLM）

XXX 发票预检的确定性实现。读取 Order Workflow 的 Team 3 待开票队列，
按锁定方案的五项检查产出 INVOICE-READY / DO-NOT-INVOICE / NEEDS-REVIEW /
CHECK-INCOMPLETE 结论。

NetSuite **只有一处写入**：队列扫描后把检查跑完的 SO 的 FORM 字段从 211 切到
219（Jing 2026-08-17 授权），仅此一个字段，前后对整条记录做全字段差异审计，
记录在 `reports/form_changes/`。CHECK-INCOMPLETE 和已开票的 SO 绝不写；手工
扫描什么都不写。

## 运行

```bash
python app.py
```

打开 http://localhost:3100/ 。工具运行期间每天 15:00 自动扫描；也可点"立即扫描"。
服务**只监听 127.0.0.1**：一轮扫描会写 NetSuite 和开票系统，而这些接口没有任何
认证，绑到局域网等于谁都能触发。要给同事用请先加鉴权，不要直接改 `BIND_HOST`。
依赖：Python 3 + openpyxl（其余全为标准库）。

## 结构

| 文件 | 职责 |
|---|---|
| `engine.py` | 纯函数检查引擎：折扣文本解析（XXX 惯用写法模式库）、decimal 复合折扣计算（HALF_UP）、五项检查、结论优先级。无网络、无副作用 |
| `sources.py` | 数据源：billing app 队列（GET /db）、参考工作簿加载与校验、NetSuite 取数接口（可替换实现） |
| `app.py` | Web 服务（端口 3100）+ 每日 15:00 调度 + 报告存档（reports/） |
| `tests/test_engine.py` | 引擎单元测试（含方案 §6.4 两个算例与 SO9000004 真实用例）。运行：`python tests/test_engine.py` |
| `tests/test_app.py` | app.py 测试（写入名单、客户生效期选行、扫描进度）。运行：`python tests/test_app.py` |
| `tests/test_sources.py` | 工作簿加载测试（多生效期客户）。运行：`python tests/test_sources.py` |

## 当前状态

- ✅ 检查引擎完成，102 项单元测试全过（engine 89 + app 10 + sources 3）
- ✅ NetSuite 取数：`netsuite_scraper.py` 通过 `&xml=T` 取结构化 XML（非 DOM 抓取），
  经 CDP 连接你常用的调试 Chrome（端口 9222，与补货/照片系统同一个），复用登录会话
- ✅ 队列读取、工作簿校验、Web UI（日期 tab / 排序 / review 原因 / INVOICED）、每日调度
- ✅ 回填：**队列扫描时自动回填**——发现"NetSuite 已开票、billing tab 仍 Unbilled"
  的 SO，自动读取发票号/金额并写回，结果记入 `reports/backfilled.json`；也可手动跑
  `backfill_preview.py` → `backfill.py`（先 dry-run 再 `--run`）
- ✅ 手动测试扫描：页面输入框可直接输入若干 SO 号即时检查（**只读，不回填**）
- ✅ 已回填过的 SO 不再进入队列扫描（读 `reports/backfilled.json` 过滤）

## 回填的写入安全（血的教训）

应用的 `DB.save()` 每次都整包上传全部订单，**逐条保存会把服务器打挂**：请求堆积挂起
不返回，而应用只在 `.then/.catch` 里累计失败数，于是挂起的请求看起来和成功一模一样。
所以 `backfill.py` 的做法是：逐条走应用的 `markBilled`（保留其校验与日志）但**抑制每条
保存**，最后**只提交一次并等待返回**，再用独立的 `GET /db` 核对，**只有服务器确认的行
才标记 BILLED**。永远不要拿浏览器内存里的状态当作写入成功的证据。
- 🔜 后期拿到 NetSuite Admin 的 Integration Token 后，新增 `RestApiSource`
  替换抓取器即可，引擎与 UI 不动

## 运行前提（重要）

**调试 Chrome 必须开着**（端口 9222），且已登录 NetSuite；回填还需要已登录的
billing app 标签页（应用登录存在 sessionStorage，是按标签页的）。Chrome 关闭时
抓取器会退回自己的空配置文件并报 `NeedLoginError`，此时所有 SO 记
CHECK-INCOMPLETE，不会误判为可开票。

## Fail-closed 原则（与锁定方案一致）

任何缺失、冲突、无法解析、未验证的证据都不允许产出 INVOICE-READY。
无法识别的折扣文本一律 NEEDS-REVIEW 交人工，绝不猜测。
规则阈值全部读自 `invoice-pre-check-reference.xlsx` 的 Rules 表。

## 已确认的业务规则（2026-08-17，Jing）

- 折扣项可用 Custom Discount 或任何预设折扣项（如 "8% Discount"），只看金额是否精确到分
- EDI 地址中星号（*）为占位符；只比对 EDI 中实际存在的字段，存在字段不一致才 FAIL
- 任何带 `-后缀` 的海外客户（-CAN/-MEX/-CSAM/-Montreal/…）多张发货单时走整车规则：
  整单材料重量 ≤ 40,000 lb 且发货单张数 ≤ 26 自动放行，任一超限才 NEEDS-REVIEW
  （阈值在 Rules 表；8/18 起数的是发货单张数，不再是托盘数）
- COD 由 Terms == "COD" 判定；COD-PR 暂不启用
- 客户主数据按 SO 日期选生效行；一个客户可以有多行不同生效期（2026-08-18）
- 「先 X% 再 Y%」式的连续折扣一律交人工，不自动计算（2026-08-18）
- **有折扣就必须有对应备注**：SO 折扣金额 > 0 而备注里没提到折扣 → NEEDS-REVIEW
  （2026-08-18；备注非空不算，要提到百分比或 disc/discount）

## 开机自启 + 看门狗（两个 Startup 脚本）

| 脚本（在"启动"文件夹） | 作用 |
|---|---|
| `ChromeDebug9222.vbs` | 拉起带调试端口 9222 的专用 Chrome，补上 NetSuite / billing app 标签页；**每 60 秒自检，端口没了就重新拉起** |
| `InvoicePreCheck.vbs` | 拉起本工具服务（http://localhost:3100/）；**每 60 秒自检，服务挂了就重新拉起** |

两个脚本都是常驻循环（在后台以 wscript 运行，无窗口），并且幂等：端口正常就什么都不做。
都用 8.3 短路径且为 ASCII 内容——WScript 按 ANSI 读取 .vbs，路径里的中文会导致
80070003 找不到路径。

**登录状态**：NetSuite 的登录存在 Chrome 配置目录里，重启后仍然有效；billing app 的
登录存在 sessionStorage（按标签页），只有 Chrome **恢复了那个标签页**时才会跟着恢复。
所以请保持 billing app 标签页开着；万一没恢复，页面顶部预检横幅会明确提示需要登录。
