# 部署手册（M9 · Windows 11）

目标：**每天 08:00 无人值守跑一次日报**，失败时能在本机一眼看出是什么坏了，
并且随时能退回上一个可用版本。

> 决策背景见 [`ADR/001-deploy-windows-task-scheduler.md`](ADR/001-deploy-windows-task-scheduler.md)。
> 命令都在仓库根目录、Git Bash 或 PowerShell 里执行。

## 0. 前提

| 项 | 检查方式 |
|---|---|
| `.venv` 已建好、依赖已装 | `.venv/Scripts/python.exe -c "import agent"` 不报错 |
| `.env` 已填好（至少 `LLM_API_KEY`） | `python -m app.main status` 不报配置错误 |
| 登录态可用 | 同上，看「登录态」那一行；没有就 `python -m app.main login` |
| 数据源是 ncss | `.env` 里 `JOB_SOURCE=ncss` |

## 1. 首次上线（四步）

```bash
# ① 先手动跑一次 —— 不通过就别注册任务
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.main report
#    退出码 0 = 成功 / 已跑过；3 = 登录过期；1 = 抓取或流程失败（含首次不可达）；
#    2 = 配置错误

# ② 看结局
PYTHONUTF8=1 ./.venv/Scripts/python.exe -m app.main status

# ③ 注册任务（每天 08:00，当前用户身份，不需要管理员）
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1

# ④ 立刻试触发一次，确认任务本身能跑通
schtasks /Run /TN "myAgent Daily Report"
```

> ③ 之后当天若已经成功跑过，④ 会因为「一天一次」返回 `already_ran`（退出码 0）——
> 这是**正确**结果，说明额度也守住了。

## 2. 日常查看

| 想知道 | 命令 |
|---|---|
| 它最近跑没跑、今天额度在不在、登录态多旧了 | `python -m app.main status` |
| 最近几天的成本与失败 | `python -m app.main board --days 7` |
| 今天的日报 | `var/reports/YYYY-MM-DD.md` |
| 业务事件（脱敏后的结构化日志） | `var/logs/app.log` |
| LLM 请求（脱敏后） | `var/logs/llm.log` |
| 任务进程的 stdout/stderr（配置错误、登录过期提示） | `var/logs/task.log` |
| 单次 run 的步骤快照 | `var/traces/run-<run_id>.jsonl` |

## 3. 退出码（对 cron / 任务计划程序的契约）

| 码 | 含义 | 该做什么 |
|---|---|---|
| 0 | 成功 / 无数据 / 今天已跑过 | 无 |
| 1 | 抓取或流程失败；或首次连接不可达 | 看 `app.log` 与 `task.log` |
| 2 | 配置错误（缺变量 / 空串 / 类型错） | 改 `.env` |
| 3 | 登录态失效或还没登录 | `python -m app.main login` 后立刻重跑 |
| 4 | 已有另一个实例在跑 | 等它结束；不要重试叠加 |

> **`unreachable`（退出码 1 的一个特例）**：第一页就在 DNS / 连接 / TLS 握手阶段失败，
> **一个 HTTP 请求都没发出去**。`status` 会显示 `status=unreachable`，并明确说
> 「不占今日额度」—— 网络恢复后**直接重跑**即可，不必等到明天。
> 判据是 `pages_done == 0`：已经抓过前几页再断网，仍按 `failed` 计、仍占额度。

## 4. 单实例与并发

- `var/app.lock` 是 **OS 级文件锁**（Windows `_locking` / POSIX `flock`）。
  进程死亡时内核自动释放，**不存在僵尸锁**；锁文件留在磁盘上是正常的。
- 任务计划程序另加了 `MultipleInstances=IgnoreNew`：自动任务之间不会叠加。
- 手动 `report` 撞上自动任务时会拿到退出码 4，直接退出。

## 5. 心跳与告警（⚠️ 部分欠账）

- 每次 `report` 走完都会写 `var/heartbeat.json`（成功、失败、登录过期都写），
  `status` 读它显示"最后一次结局 + 多久以前"。
- **外部推送（飞书）按主人决定暂缓**：人不在电脑前时，本机心跳无法主动通知。
  这是一笔明确欠账，PLAN §M9 对账里照实记着。
- 判断「是不是挂了」的可靠方法：`status` 里心跳时间超过 24 小时且今天额度
  未使用 → 任务大概率没跑起来（任务计划程序、断电、休眠）。

## 6. 回滚

```bash
git tag                      # 看可用版本
git checkout v0.9.0          # 回到上一个稳定版本（或更新的 tag）
pip install -e ".[dev]"      # 依赖可能与版本绑定
```

- **回滚不动数据**：`var/`（数据库、登录态、报告）不在 git 里，也不会被 checkout 改动。
- 停掉自动任务：`powershell -ExecutionPolicy Bypass -File scripts\unregister_task.ps1`
- 回滚后先手动跑一次 `report`（或 `eval.run_eval` 的离线回放），确认再注册任务。

## 7. 已知限制

| 限制 | 影响 | 现状 |
|---|---|---|
| 服务端会话寿命未知（M0 欠账） | 登录态可能几天后失效 | `report` 以退出码 3 明确报告，不会静默失败 |
| `offset` 是页码还是行号未复核（M0 欠账） | 若猜错，翻页会原地打转 | 隔离在 `sources/ncss._offset_for_page`，修只改一行 |
| 任务以当前用户身份运行 | 用户未登录时不执行 | `StartWhenAvailable` 会补跑；要"未登录也跑"需存密码，暂不做 |
| 外部告警未接 | 无人值守时没有主动通知 | 本机心跳 + status；飞书待接 |
