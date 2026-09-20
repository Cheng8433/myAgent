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
- [ ] M0 浏览器抓包补全站点档案
- [ ] M1 LLM 裸调用
- [ ] M2 工具层（先 remoteok，后 ncss）
- [ ] M3~M9

## 快速开始

```bash
# 1. 建虚拟环境（本机 python 与 Anaconda 的 pip 版本不一致，务必用项目内 venv 隔离）
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# .venv\Scripts\activate.bat       # Windows CMD

# 2. 安装依赖
pip install -e ".[dev]"

# 3. 配置（.env 不会被提交，可以放心填）
cp .env.example .env
#    然后编辑 .env，至少填上 LLM_API_KEY

# 4. 半自动登录（M2 完成后可用；会弹出真实浏览器让你手动过验证码）
python -m app.main login

# 5. 跑一次日报
python -m app.main run
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
