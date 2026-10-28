# Invoice Pre-Check · 发票开具前预检系统

将 NetSuite 销售订单（SO）、待开票队列与 Excel 参考规则进行确定性比对，生成可追溯的预检报告。规则引擎不依赖 LLM，适合展示 ERP 与本地业务工具之间的核对流程。

## 核心能力

- 检查订单资格、折扣、价格等级、单价、发货范围和 EDI 地址。
- 使用 Decimal 处理折扣金额与舍入，按订单日期匹配客户规则生效期。
- 输出 `INVOICE-READY`、`DO-NOT-INVOICE`、`NEEDS-REVIEW`、`CHECK-INCOMPLETE` 四类结论。
- 提供本地 Web 界面、手动 SO 核对、每日 15:00 队列扫描与报告归档。
- 缺失、冲突或无法解析的证据交人工或标记检查不完整，避免自动放行。

## 技术与结构

Python 标准库 HTTP 服务、openpyxl、Playwright/CDP、HTML/JavaScript。

| 路径 | 用途 |
| --- | --- |
| `invoice-precheck-tool/engine.py` | 纯函数规则引擎 |
| `invoice-precheck-tool/sources.py` | 队列和参考工作簿加载 |
| `invoice-precheck-tool/netsuite_scraper.py` | NetSuite 会话与结构化数据读取 |
| `invoice-precheck-tool/app.py`、`static/` | Web 服务、调度和页面 |
| `invoice-precheck-tool/backfill*.py` | 发票回填及预览 |
| `netsuite-invoice-pre-check/references/` | 工作簿结构、折扣规则与报告约定 |

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install openpyxl playwright
cd invoice-precheck-tool
..\.venv\Scripts\python.exe app.py
```

浏览器打开 http://127.0.0.1:3100 。服务运行时会按本机时间执行调度。

完整扫描需要先配置自己的业务环境：

1. 根据 `references/workbook-schema.md` 准备参考工作簿，并设置 `sources.py` 中的 `WORKBOOK` 路径。
2. 将相关代码中的 billing app 地址、NetSuite 账号和业务表单映射替换为自己的配置。
3. 准备已登录 NetSuite 的调试 Chrome（CDP 端口 9222）；回填还需要登录 billing app 标签页。

## 写入行为

本工具包含真实写入逻辑：队列扫描可将已完成检查的 SO 表单字段从 211 切换到 219，并向 billing app 回填已开票订单的发票号及金额。手动 SO 检查走只读流程。正式连接前须检查表单 ID、字段映射和扫描入口，先在测试环境验证。

接口未提供完整鉴权，保持默认 `127.0.0.1` 监听。不要将该服务直接暴露到公网。

## 脱敏版状态

真实规则工作簿、客户资料、登录状态、API 凭据、历史报告及原测试集均未提供。此版本用于阅读与二次配置，不能在缺少这些依赖时直接完成业务核对。子目录原有说明含历史测试结果，不代表本公开版本重新通过相同测试。详见 [脱敏说明](脱敏说明.md)。

仓库未附开源许可证，使用和再分发请先确认授权。
