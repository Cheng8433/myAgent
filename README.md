# myAgent · 招聘信息日报 Agent

自动抓取招聘信息、去重入库、生成 Markdown 日报，并附带日志与运行留痕；
另提供基于本地岗位库的岗位匹配（规则 / 单次 LLM / Agent 循环三种模式）。
主数据源为国家大学生就业服务平台（ncss.cn，学信网 CAS 登录），开发期可用
remoteok.com 作为零登录替身。

## 功能

- **半自动登录**：Playwright 打开有头浏览器，由人工完成图形验证码；程序只保存
  登录态（cookie），从不读取或保存账号密码。
- **抓取与翻页**：按页拉取职位，直到空页为止；原始响应体按内容寻址存档。
- **幂等入库**：SQLite 以 `(source, source_id)` 为主键做 upsert，重复抓取不产生重复行。
- **日报生成**：调用 LLM 生成两三句摘要，渲染为 Markdown 表格并落盘。
- **岗位匹配**：按求职档案在本地库检索、查看详情、排序并提交带分数与理由的清单；
  支持纯规则、单次 LLM、ReAct Agent 三种模式，可做同池 A/B 对比。
- **可观测**：结构化日志（业务 / LLM 分文件）、每次 run 的 JSONL trace、成本看板。
- **护栏**：工具白名单真正拦截执行、网页内容隔离、出站日志脱敏、实名站点一天一次
  且翻页间隔 ≥3 秒、登录失效即以退出码 3 停下等人工续期。

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 / 运行时 | Python 3.13 |
| Agent 编排 | 手写 ReAct 循环（不依赖 Agent 框架） |
| LLM | 任意 OpenAI 兼容接口（DeepSeek、通义千问等） |
| HTTP | httpx（连接复用、显式超时） |
| 数据校验 | Pydantic |
| 存储 | SQLite（WAL） |
| 日志 | structlog + 标准库 logging |
| 登录 | Playwright（Chromium，有头模式） |
| 测试 / 质量 | pytest、respx、ruff |

## 项目结构

```
myAgent/
├─ docs/                   设计文档（PLAN / SITE / DEPLOY / ADR）
├─ src/
│  ├─ agent/
│  │  ├─ llm/              LLM Provider 抽象与 OpenAI 兼容实现
│  │  ├─ tools/            工具注册表、登录态、数据源（ncss / remoteok）
│  │  ├─ runtime/          ReAct 循环、重试、预算闸门
│  │  ├─ memory/           SQLite 连接、建表、状态读写
│  │  ├─ observability/    日志、trace、成本看板、脱敏
│  │  └─ guardrails/       限速、不可信内容隔离
│  ├─ workflows/           日报主流程（抓取 → 入库 → 摘要 → 渲染）与岗位匹配
│  └─ app/                 命令行入口、单实例锁、心跳
├─ eval/                   评估用例与执行器（回放、注入防线、匹配 A/B）
├─ profiles/               求职档案模板
├─ tests/                  单元测试（离线，不发真实请求）
├─ scripts/                各阶段验收脚本与 Windows 任务计划脚本
└─ var/                    运行时数据（数据库、登录态、报告、清单、日志；已被 gitignore）
```

## 部署

前提：Windows 11 / Git Bash、Python 3.13。

```bash
# 1. 虚拟环境（务必用项目内 venv 隔离系统 Python）
python -m venv .venv
source .venv/Scripts/activate

# 2. 安装依赖
pip install -e ".[dev]"

# 3. 配置（.env 不会被提交）
cp .env.example .env
#    至少填写 LLM_API_KEY；LLM_* 与 AGENT_* 均为必填，代码中没有默认值

# 4. 安装浏览器内核（仅登录需要）
python -m playwright install chromium

# 5. 半自动登录（弹出浏览器，人工过验证码）
python -m app.main login
```

### 定时运行（Windows 任务计划程序）

```powershell
# 注册：每天 08:00 运行 scripts/run_report.cmd，错过后补跑
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1

# 取消
powershell -ExecutionPolicy Bypass -File scripts\unregister_task.ps1
```

单实例由 `var/app.lock` 保证；`MultipleInstances=IgnoreNew` 防止任务叠加。
详细运维与回滚见 `docs/DEPLOY.md`。

### Windows 中文编码

```bash
export PYTHONUTF8=1
```

代码中所有文件读写均显式使用 `encoding="utf-8"`。

## 使用

```bash
# 跑一次日报（抓取 → 入库 → 摘要 → 渲染）
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.main report

# 查看最近结局、今日额度、登录态年龄（只读）
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.main status

# 查看最近 N 天的成本与失败（只读）
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.main board --days 7

# 登录态过期后续期，然后立刻重跑
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.main login

# 岗位匹配（默认 Agent 模式；可选 workflow / single / ab）
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.main match --profile profiles/example.json --mode agent
```

切换数据源：在 `.env` 中设置 `JOB_SOURCE=remoteok`（替身）或 `JOB_SOURCE=ncss`（主靶子）。

### 输出位置

| 内容 | 路径 |
|---|---|
| 日报 | `var/reports/YYYY-MM-DD.md` |
| 业务日志 / LLM 日志 | `var/logs/app.log`、`var/logs/llm.log` |
| 单次 run 留痕 | `var/traces/run-<run_id>.jsonl` |
| 原始响应存档 | `var/raw/<sha1>.json` |
| 数据库 | `var/app.db` |
| 登录态 | `var/auth/ncss_state.json` |
| 心跳 | `var/heartbeat.json` |
| 匹配清单 | `var/shortlists/<run_id>.json`、最新一份 `latest.json` |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 / 无数据 / 今天已跑过 |
| 1 | 抓取或流程失败（含首屏不可达） |
| 2 | 配置错误 |
| 3 | 登录态失效或尚未登录 |
| 4 | 已有另一个实例在运行 |

`match` 子命令沿用 0 / 1 / 2：没有候选、模型输出不合格、模型未提交清单都算
「结果不好」而非「程序坏了」，退出码 0；运行环境失败为 1，档案或配置错误为 2。

### 测试与离线验收

```bash
./.venv/Scripts/python.exe -m pytest
./.venv/Scripts/python.exe -m eval.run_eval
./.venv/Scripts/python.exe scripts/m8_verify.py   # 其余 scripts/m*_verify.py 同理
```
