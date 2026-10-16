# myAgent · 招聘信息日报 Agent

自动登录学校招聘网 → 拉取招聘信息 → 生成日报 → 附带日志（异常时推送飞书）。

这是一个**学习型项目**：用真实可用的需求，把 Agent 工程的 8 项能力
（选模型 / 写工具 / 做编排 / 管状态 / 加评估 / 做可观测 / 保安全 / 能部署）
从「听说过」变成「写过、讲得清」。

> **完整计划书见 [`docs/PLAN.md`](docs/PLAN.md)** —— 分 M0~M9 十个里程碑，
> 每个里程碑都带「学什么 / 做什么 / 常见坑 / 自测题」。动手前请先读它。

## 技术路线

| 项 | 选择 |
|---|---|
| 编排 | **手写 ReAct 循环**，不用 Agent 框架 |
| LLM | 国内 OpenAI 兼容接口（DeepSeek / 通义等） |
| 主靶子 | `ncss.cn` 国家大学生就业服务平台（学信网 CAS 登录） |
| 开发期替身 | `remoteok.com/api`（零登录、零对抗，供 M1~M3 快速迭代） |
| 登录 | **半自动**：人工过一次图形验证码 → 复用登录态 → 失效时告警 |

## 当前状态

- [x] 计划书 `docs/PLAN.md`
- [x] 仓库地基：目录骨架、`.gitignore`、`.env.example`、`pyproject.toml`
- [ ] M0 浏览器抓包补全站点档案（`docs/SITE.md` 大头都到手了：CAS 登录字段、职位接口路径与分页参数、响应结构、**登录态 cookie 实测快照** —— 业务会话是 `SESSION` @ `www.ncss.cn` `/student/`，`www.ncss.cn` 域共 13 条、CAS 域 8 条按域名排除；**还差两格**：`§4` `offset` 是页码还是行号 / `§5` **服务端**会话寿命 —— 后者有一不利线索：`SESSION` 是会话级 cookie（`expires=-1`），服务端超时未知，而同名的 `JSESSIONID`（CAS 域）正是 Tomcat 默认名、默认仅"30 分钟不活动"，需"今天登、明天跑"才能定）
- [x] M1 LLM 裸调用 —— `scripts/m1_verify.py`
- [x] M2a 工具层 · remoteok 替身 —— `scripts/m2_verify.py`
- [ ] M2b 工具层 · ncss 真源 —— **Phase A + Phase B 代码均已落地，且已用真实登录态单页实证**：`python -m app.main login` 半自动登录 + 登录态落盘（`agent/tools/auth.py`）+ 登录过期接线（`status=login_expired` → 退出码 3）；`sources/ncss.py` 照 `docs/SITE.md` 实现解析与分页，`tests/test_ncss.py` 29 条离线断言（respx 假响应，**一次真实请求都不发**）。✅ **单页端到端已实测（2026-10-16）**：真实登录态 + 一次 GET → `flag=true`、10 条、raw 7425 字节（与档案分毫不差）。⚠️ **整份日报仍未真跑**：翻页节流（≥3s）属 M8，未实现前跑完整 flow 会背靠背拉页，违反本项目合规规则（详见 PLAN §M2 Phase B 对账第 6 条）
- [x] M3 ReAct 主循环 —— `scripts/m3_verify.py`（第 8 项走真模型 + 真工具）
- [x] M4a 编排加固：重试 / 超时 / 预算闸门 —— `scripts/m4_verify.py`
- [x] M4b 确定性主流程：抓取 → 两阶段入库 → 摘要 → 渲染落盘 —— `scripts/m4b_verify.py`
- [x] M5 状态管理：SQLite 建库 + 幂等写入 + 内容寻址存档 —— `scripts/m5_verify.py`
- [x] M6 可观测：三层日志（`app.log` / `llm.log` / `var/traces/*.jsonl`）+ 成本看板 —— `scripts/m6_verify.py`
- [x] M7 评估：三层（L1 `pytest` 回归网 · L2 离线回放 · L3 摘要性质断言）—— `pytest` + `python -m eval.run_eval`
- [ ] M8 安全与推送 · M9 部署

> **M4b 留下的两件事**：① 真链路只跑过一次手工验证，还没被任何脚本盯住 ——
> `m4b_verify.py` 全程离线（假数据源 + 假 LLM），真实网络/模型的形状由
> `python -m app.main report` 手工确认。② 摘要那一趟如果 LLM 直接抛错，
> 会走 `_summarize` 里那条 `except LLMError` 降级（日报照出，只有摘要是占位符）；
> 这一支的 token 用量仍然只能记 0（异常没带回来）—— 但 M6 起失败这件事本身有了留痕
> （一条 `kind="subrun"` / `error="llm_error"` 的 StepRecord 落进 trace）。
> **正常路径**的用量现在同时进 `runs` 两个列与 trace 的 llm 行。

## 快速开始

```bash
# 1. 建虚拟环境（本机 python 与 Anaconda 的 pip 版本不一致，务必用项目内 venv 隔离）
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# .venv\Scripts\activate.bat       # Windows CMD

# 2. 安装依赖
pip install -e ".[dev]"

# 3. 配置（.env 不会被提交，可以放心填）
#    ⚠️ 这一步是【必需】的：预算与超时都是必填配置（AGENT_* / LLM_TIMEOUT_SECONDS），
#       代码里没有默认值 —— 没有 .env，程序在启动时当场报错，而不是悄悄用别的数。
cp .env.example .env
#    然后编辑 .env，至少填上 LLM_API_KEY

# 4. 半自动登录（M2b 完成后可用；会弹出真实浏览器让你手动过验证码）
python -m app.main login

# 5. 跑一次日报（在仓库根目录执行）
python -m app.main report
```

## ⚠️ Windows 中文编码（务必先看，否则会白踩坑）

本机 Python 3.13 在 Windows 上**默认用 cp936(GBK)** 处理文本，而本项目通篇是中文
（日报、日志、岗位标题）。不处理会出现两类症状：

| 症状 | 原因 |
|---|---|
| 控制台输出乱码 `���Ĳ���` | stdout 按 GBK 编码，终端按 UTF-8 解码 |
| `UnicodeEncodeError: 'gbk' codec can't encode character` | `open()` 没指定 encoding，默认落到 GBK |

**两条纪律：**

1. 每次开终端后设一次 UTF-8 模式（写进 shell 配置更省事）：
   ```bash
   export PYTHONUTF8=1        # Python 3.7+ 的 UTF-8 Mode，一劳永逸
   ```
2. 代码里**所有** `open()` 都显式写 `encoding="utf-8"`。
   读网页、写日报、写 trace、写日志，一处都不能漏。
   > 这不是洁癖：Windows 上 `open()` 不写 encoding 就等于赌运气，
   > 而 M5 的日报落盘、M6 的 trace 写入全都涉及中文。

## 安全须知

- `.env`、`var/auth/`（登录态）、`var/`（运行时数据）**均已被 `.gitignore` 挡住**。
  提交前请习惯性 `git status` 自查。
- ncss 背后是**实名学信网账号**，本项目**不做**任何反爬绕过：
  不接打码平台、不逆向验证码、不伪造设备指纹。
  登录由主人亲自完成，程序只保存登录态。
- 抓取频率硬性限制为**一天一次**，翻页间隔 ≥3 秒。
  出错即停手告警，不重试轰炸。
- 推送默认关闭（`SEND_NOTIFY=false`），本地开发不会往外发消息。

## 目录结构

```
myAgent/
├─ docs/PLAN.md        ← 计划书，先读它
├─ docs/ADR/           ← 架构决策记录（每个决定写一条，面试素材）
├─ src/agent/          ← Agent 内核：llm / tools / runtime / memory / observability / guardrails
├─ src/workflows/      ← 确定性主流程（日报骨架）
├─ src/app/            ← CLI 入口与定时调度
├─ eval/               ← 评估用例与执行器
├─ tests/              ← 单元测试
└─ var/                ← 运行时数据（已被 gitignore）
```
