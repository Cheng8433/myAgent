# ncss.cn 站点档案（M0 侦察记录）

> **这是 M0 的作业纸，不是已完成的档案。** 带 `❓` 的格子必须在**真实浏览器**里手工补 ——
> curl 能告诉你"要登录"，告诉不了你"登录后长什么样"，而这一层信息是 M2 那句
> `fetch_job_list` 的**唯一依据**（PLAN §M0）。
>
> ⚠️ **凭据一律写 `<REDACTED>` 占位**，绝不放真值。学信网是**实名账号**，
> 泄露后果远重于普通账号。记完顺手跑一次 `git status` 自查。

---

## 1. 已实测确定的事实（2026-09-16，本机 curl）

| 项 | 值 |
|---|---|
| 站点名称 | 国家大学生就业服务平台（NCSS，教育部官方） |
| 首页 | `https://www.ncss.cn` → HTTP 200（服务端渲染 Thymeleaf） |
| 登录入口 | `https://www.ncss.cn/student/login.jsp` → 302 |
| 登录终点 | `https://account.chsi.com.cn/passport/login?service=https://www.ncss.cn/student/connect/chsi&entrytype=stu` |
| 登录方式 | 学信网 CAS 统一身份认证（账号密码，非短信） |
| 是否有验证码 | ⚠️ **两处记录打架，待复核**：本行（2026-09-16 手工观察）写"有，图形验证码"；而 §3.1 真机抓到的那条 POST **payload 里根本没有验证码字段**。→ 复核方法：**退出登录 + 清掉 ncss/chsi 的 cookie，再登一次**，看是否弹验证码。我的猜测是**条件触发**（新设备/异地/密码错多次才弹），但这是猜的，`<REDACTED>` 不写进档案。**这条影响 §7 里的半自动登录理由**——见 §3.1 末行 |
| 列表页 | `https://www.ncss.cn/student/jobs/index.html`（**前端 JS 渲染**，数据靠登录后接口） |
| 详情页模板 | `/student/jobs/{jobId}/detail.html`；企业页 `/student/jobs/{corpId}/corp.html` |
| 招聘会 | `/student/jobfair/index.html` |
| 免登录接口 | `/student/api/news/list?limit=8` → 200 JSON（就业资讯） |
| 反爬观测 | **未见签名参数；未登录一律 302 到登录页 —— 主要门槛是登录，不是反爬** |

### 统一底座（为什么选它当靶子）

清华就业网的首页链接直接指向 `thu.ncss.cn/student/jobs/index.html` —— 说明 NCSS 是
众多高校就业网的**统一底座**。接一个 ncss 源，等于拿到一整片高校就业网的入口。

---

## 2. ✅ 已完成的五格（记录见 §3 / §4）

| # | 要记的东西 | 怎么记 | 填在这里 |
|---|---|---|---|
| 1 | CAS 登录表单 **POST 到哪个 URL、带哪些字段** | Network → 点登录 → 看那条 POST 的 Payload | → §3 |
| 2 | 登录成功后**哪个 Cookie 名下发了** | Application → Cookies，对比登录前后的差异 | → §3 |
| 3 | **职位接口的真实路径** | 登录后开列表页，Network 里筛 XHR/Fetch | → §4 |
| 4 | **分页参数名**（可能是 POST body 而不是 GET 参数） | 点第 2 页，对比两次请求的差异 | → §4 |
| 5 | **返回 JSON 的结构** | 复制 Response 的**前一两条**（别贴整包） | → §4 |

> 💡 **第 2 格可以偷个懒**：`python -m app.main login` 跑成功后，它会把捕获到的
> cookie 名直接打出来 —— 那正是这一格要的内容。

---

## 3. ❓ 登录流程记录

### 3.1 CAS 表单

| 项 | 记录 |
|---|---|
| POST URL | https://account.chsi.com.cn/passport/login?service=https://www.ncss.cn/student/connect/chsi&entrytype=stu|
| 请求方法 / Content-Type | POST |
| `username` / `password` 的值 | `<REDACTED>` —— 本次是主人在浏览器窗口里**手输**的，代码和文件里不留真值（这正是半自动登录的理由之一） |
| `lt`（CAS 票据）**从哪来** | ★ **答案在登录页的 HTML 里**，不在前一个请求的响应头里。GET 登录页 → HTML 里藏着 `<input type="hidden" name="lt" value="LT-<REDACTED>-cas">`。形状：`LT-<6位数字>-<21位随机>-cas`。**一次一换，用完即废**（CAS 的 login ticket，只在"渲染表单→提交表单"这一个来回内有效） |
| `execution` **从哪来** | **同一个登录页 HTML 的另一个隐藏字段**：`<input type="hidden" name="execution" value="<UUID>_<超长 base64>">`。那个 base64 解出来是**两层的**：外层裸 base64 → 一段 `{"alg":"HS512"}` 的 JWT，而 payload 是**加密密文**（实测 5202 字节，里面没有明文账号/密码/邮箱）。它是 CAS 的**一次性 webflow 会话状态**，寿命以分钟计 |
| `_eventId` 的值 | `submit` |
| 有没有验证码字段 | 本次登录 **POST payload 里没有任何验证码字段**（`captcha` / `verifyCode` / `checkCode` 一个都没有）→ 与 §1 打架，**已标 ⚠️ 待复核**。★ 对设计的影响：这条**不该**推翻半自动登录 —— 半自动的理由是"**实名账号的密码不进代码/日志/仓库**" + "CAS 多步跳转本身脆弱"，验证码只是加了一条。若复核确认真的没有验证码，§7 的措辞要改，但**结论不变** |

### 3.2 登录终点的交换

| 项 | 记录 |
|---|---|
| POST 到 CAS 之后跳转链路（按顺序列 URL） | **①（实测）** `GET  https://www.ncss.cn/student/login.jsp` → 302 → `account.chsi.com.cn/passport/login?...`<br>**②（实测）** `POST https://account.chsi.com.cn/passport/login?service=.../connect/chsi&entrytype=stu` → **302**，`Location: https://www.ncss.cn/student/connect/chsi?ticket=ST-<REDACTED>-cas`<br>**③（待确认）** `GET .../student/connect/chsi?ticket=ST-...` → ncss **拿 ticket 去 CAS 后端校验**（server-to-server，浏览器里看不到这一跳）→ **Set-Cookie 下发自己的会话** → 302 到 `/student/index.html`<br>**④（待确认）** `GET /student/index.html` → 200 |
| 回跳 `connect/chsi` 时换了什么 | ★ **"换了什么"= ① CAS 的一次性 ticket 被换成了 ② ncss 自己的一张长命会话 cookie。**<br>这就是 CAS 的 **Service Ticket 交换**：`ST-...-cas` 是 CAS 发给"服务方"（这里是 ncss）的**一次性凭证**，只能用一次、几十秒失效；ncss 拿着它向 CAS 的 `/serviceValidate` 校验一次（这步是服务器之间通信，浏览器抓不到），确认"这个人确实是刚通过认证的"，然后**签发给自己的 session cookie** —— 之后所有请求都只认这张 cookie，和 CAS 再没关系。<br>**怎么亲眼确认**：DevTools → Network → 勾上 **Preserve log**（跨跳转保命，不勾的话 302 会把你刚要看的那条刷没）→ 清空 → 重新登录 → 找到 `connect/chsi` 那条 → 看 **Response Headers**：`Location`（它往哪跳）与 `Set-Cookie`（它发了什么）。 |
| **登录态 Cookie 名** | ✅ **实测（2026-10-16，`python -m app.main login` 亲跑）**：真正的业务会话候选是 **`SESSION`**（`www.ncss.cn` / `/student/`，HttpOnly ✓ Secure ✓）。<br>⚠️ **`JSESSIONID` 也真实存在 —— 但它属于 `account.chsi.com.cn`（CAS 服务器），不属于本站。** 本格最初写成 JSESSIONID 是**转述失真**：DevTools 的 Application 面板把各域 cookie 平铺在同一张表里，容易读到 CAS 那一组。**实测快照（见下一格）是唯一的准。**<br>★ **`www.ncss.cn` 域实测共 13 条**（也就是 `login` 末尾打印的那 13 个）：`SESSION` / `XSRF-CCKTOKEN` / `CHSICC01` / `CHSICC_CLIENTFLAGSTUDENT` / `acw_tc` / `aliyungf_tc` / `cna` / `_ga` / `_ga_6CXWRD3K0D` / `HMACCOUNT` / `Hm_lvt_…` / `Hm_lpvt_…`（混着 WAF 与三套埋点）。<br>★★ **"到底哪一条才是会话"这个问题我们【不回答】。** 代码把 13 条**整袋带走**（按域名子串），所以 `SESSION` 与 `CHSICC01` 谁是真身**根本不影响运行**。这不是偷懒，是取舍：**依赖"必须挑对那一条"的设计，会在 cookie 改名的某天静默失效；"整袋带"没有这个失效模式。**<br>**复核方法**（哪天突然失效时重查）：跑 `python -m app.main login`，它把捕获到的 cookie 名**直接打出来**。 |
| 该 Cookie 的 domain / path / 是否 HttpOnly | ✅ **实测（2026-10-16，快照 21 条摊开 · 值一律不入档）**：<br>**① 我们带走（13 条，domain 含 `ncss.cn`）** —— `SESSION` · `www.ncss.cn` · **`/student/`** · HttpOnly ✓ · Secure ✓ · `expires=-1`（**会话 cookie**）；`XSRF-CCKTOKEN` · `/student`；`CHSICC01` · `/` · HttpOnly ✓；`CHSICC_CLIENTFLAGSTUDENT` · `/`；外加 WAF（`acw_tc` / `aliyungf_tc`）与埋点（`cna` / `_ga` / `_ga_6CXWRD3K0D` / `HMACCOUNT` / `Hm_lvt_*` / `Hm_lpvt_*`）。<br>**② 我们丢掉（8 条，domain 不含 `ncss.cn`）** —— `JSESSIONID`、`CASTGC`、`CASPRIVACY`、`CHSICC_CLIENTFLAGPASSPORT`（全在 `account.chsi.com.cn`）；`_ga` / `_ga_TT7MCH8RRF`（`.chsi.com.cn`）；`HMACCOUNT_BFESS`（`.hm.baidu.com`）；`cna`（`qtedu.om.moe.edu.cn`）。<br>★★★ **`JSESSIONID` 在这张表里是被【排除】的那个，而这是设计不是巧合**：CAS 那台（`account.chsi.com.cn`）自己也跑 Tomcat、也发一个 JSESSIONID —— **同名，只靠 Domain 区分**。这正是"域名是筛子、名称是答案"的兑现。<br>★ **`SESSION` 的 Path 是 `/student/`（不是 `/`）**：若我们依赖路径匹配，Path 记错就会**静默漏发**；而我们发的是**扁平 `Cookie:` 头**（httpx 不做路径匹配），13 条原样出去 —— 这一格又一次证明"不挑、整袋带"是对的。<br>★ 顺带认出 **`CASTGC`** = CAS 的 **Ticket Granting Cookie**（SSO 的长期免登录票），与 `JSESSIONID` 同在 `/passport`，一起被排除。<br>★ `SESSION` 与 `CHSICC01` 都是 HttpOnly，**谁是"真身"我们不需要知道**（见上一格）；`expires=-1` 即"会话级"，服务端寿命见 §5。 |

> **怎么自己复核第 ③ 跳**：DevTools 的 Network 面板默认**只显示当前页面所在的导航**，
> 302 会把上一条冲掉。所以抓登录链的**唯一正确姿势**是勾上 **Preserve log**，
> 然后把筛选设成 `All`（不是 XHR），重新登录一次，整条 302 链就会连着头都留着。

> ★ **顺手把一个概念立在档案里：cookie 有【两个方向】，别混。**
> · **发下来**：响应头里的 `Set-Cookie`（服务端 → 浏览器）。第 ③ 跳就是这样把
>   ncss 的会话 cookie 塞进浏览器的。
> · **带上去**：请求头里的 `Cookie`（浏览器 → 服务端）。之后每一次访问职位接口，
>   浏览器／`httpx` 都靠它自报家门。
> · **`storage_state()` 拍的快照 = 浏览器那只 cookie 罐子（jar）**，即"收下来的全部"；
>   我们只从罐子里挑**属于 ncss 域**的那些，再照着上面的方向**带上去**。
> · 所以 **`value` 是会变的**（每次登录换一张新"票"），**值不进档案**；
>   档案只记**名称 + 域名 + Path + 标志位** —— 这些才是稳定的部分。

> ⚠️ 登录后前端可能**有二次请求换取 token**，别只抓第一个请求；有些站点用的是
> `Authorization` 头而不是 Cookie。

---

## 4. 职位接口记录（已填 —— 只剩"`offset` 到底是页码还是行号"待复核）

| 项 | 记录 |
|---|---|
| 完整路径（含前缀） | `https://www.ncss.cn/student/jobs/jobslist/ajax/`（**注意末尾那个斜杠**，去掉会 404）<br>后面全是**筛选参数**，实测全传空串即可：<br>`?jobType=&areaCode=&jobName=&monthPay=&industrySectors=&property=&categoryCode=&memberLevel=&recruitType=&offset=1&limit=10&keyUnits=&degreeCode=&sourcesName=0&sourcesType=`<br>⚠️ 末尾那个 `&_=1792149378213` 是 **jQuery 自动加的防缓存时间戳**，**不要抄**（写死一个旧时间戳等于告诉对方/CDN"我要的就是那份老东西"）—— 要脱敏掉，或换成当前毫秒。 |
| 方法（GET / POST） | **GET**（不是 POST —— 抓到的那条明确是 GET，这点和常见的"POST 分页"猜测相反） |
| 分页参数名 + 起始页 | **`offset`**。起始是 **1**（实测 `offset=1` 就是第 1 页）。★ **但它到底是"页码"还是"起始行号"，必须用第 2 页验一次**：再点一次第 2 页，如果发出去的是 `offset=2` → 是**页码**；如果是 `offset=11` → 是**行偏移**。**两者在第 1 页长得一模一样**，猜错了会静默地重复抓第 1 页、或整页错位。 |
| 每页条数参数名 / 上限 | **`limit`**。实测 `limit=10` 拿到 10 条。上限未知（档案默认口径 `limit=20`，Phase B 试一次 `limit=100` 看它给不给）。 |
| 其他必需参数（城市？关键词？） | **没发现必需的** —— 那把筛选参数传空串照样返回结果。**城市=`areaCode`、关键词=`jobName`**（值不是城市名而是**编码**，Phase B 若要做筛选得另外抓一次带筛选的请求）。★ 最小可用请求 = 只带 `offset` + `limit`，其余全空。 |
| 返回是**数组**还是**带分页信息的对象** | **对象**（不是裸数组）。形状：`{flag, global, errors, data:{list:[…], pagenation:{…}}}`。<br>★ 与 remoteok **相反**：那边 `raw[0]` 是声明对象、职位从 `raw[1]` 起；这边没有声明对象，职位全在 **`data.list`** 这层里 —— 坑换了个位置。<br>★ `flag` / `errors` 是**业务层**的成功标志（HTTP 200 不代表成功），见 §4.3 第 3 条。 |
| ★ **数组第一个元素是不是职位** | **是** —— `data.list[0]` 直接就是一条职位，不用像 remoteok 那样先筛掉声明对象。 |
| 单条职位里：id / 标题 / 公司 / 链接 / 城市 / 发布时间 的**字段名** | `jobId` / `jobName` / `recName` / ⚠️**没有这个字段，得用 `jobId` 拼** / `areaCodeName` / `publishDate`（⚠️ **毫秒 epoch 整数**，不是 ISO 串）<br>→ 完整映射见 §4.2 |
| 单条响应体大约多大 | **约 7.4 KB / 10 条**（紧凑 JSON 实测 7425 字节，约 0.7 KB 一条，单条 24 个字段）。 |

> **样本原文不在这里贴了** —— 完整的一份已经落盘进仓库：
> **`eval/fixtures/ncss_sample.json`**（10 条全留着，字段名、类型、`pagenation`
> 的错拼都与线上一致）。`tests/test_ncss.py` 就是拿它当离线回放的原料。
>
> ★ 为什么档案里不再贴副本：那和"同一个事实两个出处"是同一种病 ——
> 改了一处、另一处不会跟着变，**而且不报错**。贴着贴着，档案讲的就和
> 测试真正喂给代码的东西分家了。原文只有一个出处，这里只留**指针**。

### 4.1 从抓包直接读出来的三条（其余看 §4.2 的映射表）

1. `data.list` 里是**职位对象**，`data.list[0]` 就是第一条 —— 没有声明对象。
2. `data.pagenation` 是源站**拼错的**（正确拼写 `pagination`，少个 `i`）—— 见 §4.3。
3. `publishDate` / `updateDate` / `sortPriority` 都是**毫秒 epoch 整数**；
   而 remoteok 那边 `date` 直接就是带 UTC 偏移的 ISO 字符串 —— 见 §4.2 那一行。

（出处：2026-10-16 本机真机抓包，`offset=1&limit=10` 那一次。）

### 4.2 ★ 归一化映射表（`sources/ncss.py` 已照此实现 —— 断言在 `tests/test_ncss.py`）

`Job` 的形状见 `src/agent/tools/sources/base.py`。两个源**字段只取交集**，源独有的进 `extras`：

| `Job` 字段 | ncss 的字段 | 备注 |
|---|---|---|
| `source` | — | 常量 `"ncss"` |
| `source_id` | `jobId` | **本来就是 str**（`"BUr3PaTy3caCeABbpm7mar"`），不用再 `str()` |
| `title` | `jobName` | |
| `company` | `recName` | |
| `url` | ⚠️ **响应里没有这个字段** | 得**自己拼**：`https://www.ncss.cn/student/jobs/{jobId}/detail.html`（模板见 §1）。★ remoteok 那边是现成的 `url` 字段，这边不是 —— 这一格是"两个源长得像、实际不一样"的典型 |
| `location` | `areaCodeName` | **值可能是 `"全国"` 而不是城市**。照抄没问题，但日报里要能容忍它 |
| `published_at` | `publishDate` | ⚠️⚠️ **毫秒 epoch 整数**（`1791796047304`），**不是** ISO 字符串！`base.py` 的契约是"ISO8601 带 UTC 偏移"，所以必须 `datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()`。**不转的后果**：ncss 的数据和 remoteok 的历史数据格式不同却静默共存 —— 不报错、不入不了库，只是**排序和按日期筛选全部悄悄失效** |
| `extras` | `lowMonthPay` / `highMonthPay` / `degreeName` / `recProperty` / `recScale` / `recTags` / `major` / `headCount` | 源独有的一律进 `extras`（对应 remoteok 的 `tags` / `salary_min` / `salary_max`） |

**"数组第一个元素是不是职位"** → **是**，`data.list[0]` 直接就是一条职位。
★ 这点和 remoteok **正相反**（那边 `raw[0]` 是 `{"last_updated":...}` 的声明对象，得按内容筛掉）。
所以 `ncss.py` **不需要** remoteok 那两道"声明对象防线" —— 但要防**另一个**东西：`data.list` 这层嵌套（见下）。

### 4.3 ★ 三个坑（照抄，别"顺手改对"）

1. **`pagenation` 是源站拼错的**（少了 `i`，正确拼写是 `pagination`）。
   → **将来真要读它，必须照抄这个错拼**。改成正确的拼写：不报错、不抛异常，
   只是 `KeyError` 或永远拿到 `None` —— 分页信息静默丢失。
   （和 PLAN §M0 "抓包是唯一依据"是同一条纪律：**源站是什么样就写什么样**，
   不要把我们的常识强加给它。）
   > ✅ **但现在还不用读它**：`fetch_all_pages` 靠"返回空页"收手，不需要总页数。
   > `sources/ncss.py` 因此**刻意没有**去碰 `pagenation` —— 照抄一个拼错的 key
   > 进代码却不消费它，就是又造一个"声明了但没人用"的空壳。
   > 这一坑先记在这里，等真有人需要总页数时再照抄。

2. **`count` 和 `total` 的含义与直觉反着**。实测同一个响应：
   ```
   "pagenation": {"count": 500, "total": 50, "limit": 10, "offset": 1}
   ```
   `count`(500) ÷ `limit`(10) = `total`(50) → 所以 **`count` = 总记录数，`total` = 总页数**。
   → 真要判"还有没有下一页"，用 `total`（页数）或 `offset < total`，**不是** `offset < count`。
   用错了会在第 50 页之后继续翻，一路发到空响应。（同上：现在没人读它。）

3. **HTTP 200 不等于成功**。响应自带业务层标志：`flag`（成功为 `true`）+ `errors`（失败时在这）。
   → ★ **这一条【已经】被消费了**，而且它比原先设想的探针更直接：
   `sources/ncss.py` 在 `flag is not True` 时直接抛 `SourceError`。
   为什么必须抛、不能按"没有岗位"处理 —— 见 `tests/test_ncss.py` 里
   `test_flag_false_is_an_error_not_an_empty_page` 那段说明：
   读成空的，日报会安静地宣布"今天没有新职位"，**一句听起来完全正常的假话**。
   → 登录失效的判据也落定了：`follow_redirects=False` + **302 落点含 `login`**
   （对 `/student/login.jsp` 与 CAS 两种答案都成立，所以不必等 §5 那一格填完）。
   ⚠️ `flag` 何时为 false **仍未实测**；已知风险是分页越界也可能返回 false —— 见
   `ncss.py` 里那段消息（真发生时要先看 `errors` 说了什么再改）。

---

## 5. ❓ 登录态生命周期

| 问题 | 记录 |
|---|---|
| 今天登的，明天还在吗？ | ★ **2026-10-20 实证补充（把下界推到了 92 小时）**：2026-10-16 保存的登录态，在 92.2 / 92.4 小时后仍能通过带 cookie 的 GET 拿到 `flag=true`（单页 10 条）；同日整份 run 也成功抓到了前 2 页（第 3 页才因 TLS 握手超时中断，与登录态无关）。所以服务端会话**至少活过 3 天 20 小时**——这仍不是"永久有效"的结论，但"每天都要人工续期"的担心已被排除。<br>⚠️ **仍未知 —— 而且 2026-10-16 拿到一条不利的线索。**<br>★ **实测**：该 cookie 的 `Expires = 会话（Session）`，即**没有 `Max-Age`/`Expires` 属性**，由浏览器**存在内存里、关浏览器即丢**。<br>★★ **但"关浏览器即丢"这一条对【我们】不成立**：我们不等浏览器，而是把值快照进 `var/auth/ncss_state.json`（`storage_state` 会把它记成 `expires: -1`），之后用 `httpx.get(cookies=...)` **手工重放** —— 重放时 httpx 也不看有效期。**浏览器侧的行为整个与我们无关。**<br>★★★ 真正决定"能用几天"的是**服务端**的 session 超时，而它**在浏览器里看不到**。⚠️ 这里有一条**必须当心的风险**：`JSESSIONID` 是 **Tomcat/Jetty 的默认会话名**，而它们的**默认 session 超时是"30 分钟不活动"** —— 若 ncss 没有调大，那么"人工登一次、之后几天自动跑"这条**前提就不成立**（变成每天都要人工续期）。<br>**唯一的硬判据**（在真跑一次之前，这条不许写成结论）：今天跑一次 `python -m app.main login`，**明天**直接 `python -m app.main report`，看退出码是不是 **3**。是 3 = 会话很短；不是 = 够用。<br>★ **已实证（2026-10-16 当天）**：拿刚落盘的登录态向 `…/jobslist/ajax/` 发了**一次** GET → `flag=true`、10 条、raw 7425 字节 —— **登完当场即可用**。于是"能不能用"这一维已被证实，未知的只剩"**能活多久**"。 |
| 失效时的信号是什么？ | ★ 这条**已经是实测的**了，不是待办：§1 那行"未登录一律 302 到登录页"就是它 —— 代码里 `LoginExpired` 的 docstring 和 `base.py:95` 都按它写好了。**只剩一个待确认**：失效时 302 的目标到底是不是 `/student/login.jsp`（还是先 302 到 CAS）。→ 抓一次失效请求的 `Location` 就能定。 |
| 有没有"滑动续期"（每次请求都刷新有效期） | 未知。★ 判据：抓两次相隔一会儿的请求，看响应头里的 `Set-Cookie` 有没有**再发一次**（发了 = 会续期）。 |

---

## 6. 日报成功的定义（可断言，不靠感觉）

> 这五条是 PLAN §M0 定下的验收标准：**"成功"必须是能写成断言的事实**。
> M7 的评估网就照这个形状长出来。

```
A. var/reports/YYYY-MM-DD.md 存在且非空
B. 报告中岗位条数 >= 1
C. 每条岗位包含 title / company / url 三个字段且非空
D. 本次运行在 traces/ 下有对应 JSONL，且状态为 success
E. 数据库中本次运行新增岗位数 = 报告中岗位数
```

---

## 7. 合规与账号安全前置（写代码时要照抄进注释）

- 只用**主人自己的**学信网账号；它是**实名账号**。
- 请求频率：**一天一次**；一次运行内翻页间隔 ≥3s + `uniform(0.5, 2)` 抖动。
  ★ 两个数都**硬编码**在 `src/agent/guardrails/ratelimit.py`，**不是配置项**
  —— 可配的护栏不是护栏（`.env.example` 里原先那对 `HTTP_*_DELAY_SECONDS`
  从未被代码读过，M8 已删除）。
- **robots.txt 实测（2026-10-20）**：`GET https://www.ncss.cn/robots.txt`
  返回 **HTTP 200，但正文是站点自己的 404 HTML 页**（软 404：content-type
  被写成 `text/plain`、size 4234、标题 `404 Not Found`）→ **ncss 没有
  robots.txt**，因此也没有显式禁止。★ 但"没有 robots.txt" ≠ "可以随便抓"：
  我们执行的是**自定的更严标准**（一天一次、翻页 ≥3s + 抖动）。
  此条只记录事实，**不做运行期抓取** —— 那会多一次请求、多一份解析，没有收益。
- **网络观测（2026-10-20）**：到 `www.ncss.cn` 的 **TLS 握手间歇性 30s 超时**
  （`_ssl.c:1001: The handshake operation timed out`）。同一时段内：`curl` 首页正常、
  两次单页 GET 成功、整份 run 抓完 2 页后第 3 页握手超时。判据是**边缘抖动**，
  不是登录态或代码问题。处置：**停手，等下一次**（重试层已经做了 3 次指数退避，
  不连环重跑）。代码上只有「第一页就不可达」不占额度（`SourceUnreachable`）；
  中途断网仍记 `failed`、仍占当天额度。
- **连接复用实测（2026-10-20）**：同一页、同一份 cookie、间隔 3.5~5s 的 A/B —
  每页新建客户端 **6/6 成功**（无 403/429/连接重置）→ **没有证据支持"ncss
  限制客户端反复重建"**；响应头 `Connection: keep-alive`，把客户端的
  `keepalive_expiry` 从默认 5s 放宽到 30s 后，**空闲 8s 仍复用同一条连接**。
  结论：整份日报的握手次数从「每页一次」降到「整轮一次」；`sources/ncss.py`
  因此持有一条 `httpx.Client`（`max_connections=1`、`KEEPALIVE_EXPIRY_SECONDS
  =30s`），由调用方 `close()`。★ 第一轮 A/B 曾误判为"服务端不让复用"：那是
  httpx 默认 5s 过期把连接丢了 —— 复用窗口必须**大于**翻页间隔上限（3+2=5s）。
- **不绕过权限、不抓取非公开内容、数据仅个人使用。**
- ❌ **不接打码平台、不逆向验证码、不伪造设备指纹** —— 验证码交给主人这个真人过
  （PLAN §M2「半自动登录」的完整理由）。这是**架构判断**，不是技术妥协。
- 凭据只进 `.env` 或 `var/auth/`（两者都被 `.gitignore` 拦着）；一旦误提交**立即改密**。
