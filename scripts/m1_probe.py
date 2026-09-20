"""M1 探针 —— 亲眼看清 tool calling 的原始协议。

这不是产品代码，是学习用的显微镜。跑它，读输出，回答问题。
运行方式（Windows 必须带 PYTHONUTF8=1，见 README）：

    PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/m1_probe.py

你的任务：把标着 TODO 的 5 处填完。样板代码已经写好了。
"""

import json

import httpx
from dotenv import dotenv_values

# ══════════════════════════════════════════════════════════
#  样板：读配置（凭据只从 .env 来，不写死在代码里）
# ══════════════════════════════════════════════════════════
cfg = dotenv_values(".env")
BASE_URL = cfg["LLM_BASE_URL"].rstrip("/")
MODEL = cfg["LLM_MODEL"]
API_KEY = cfg["LLM_API_KEY"]

if not API_KEY:
    raise SystemExit("LLM_API_KEY 还没填，请先编辑 .env")


# ══════════════════════════════════════════════════════════
#  样板：一次 POST
#  ★ 请盯住这个函数看十秒：所谓"调用大模型"就是这么一次 HTTP 请求，
#    没有会话、没有上下文 ID、没有魔法。模型是无状态的。
# ══════════════════════════════════════════════════════════
def post(payload: dict) -> dict:
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=payload,
        )
        print(f"    [HTTP {resp.status_code}] POST {BASE_URL}/chat/completions")
        if resp.status_code != 200:
            # ★ 服务端的报错正文通常直接告诉你哪个字段不对。
            #   别让它被 raise_for_status() 的异常吞掉，否则只能盲猜。
            print("    ⚠️ 服务端返回的错误详情：")
            print("    " + resp.text[:800])
        resp.raise_for_status()
        return resp.json()


# ══════════════════════════════════════════════════════════
# TODO 1 —— 定义两个工具
#
# 形状（这是 OpenAI 的协议，照抄骨架即可）：
#   {"type": "function",
#    "function": {"name": "...",
#                 "description": "...",
#                 "parameters": {"type": "object",
#                                "properties": {"city": {"type": "string",
#                                                        "description": "..."}},
#                                "required": ["city"]}}}
#
# 两个工具：get_weather(city) 和 get_time(city)，参数都是 city。
#
# ⚠️ description 要认真写。它不是给人看的注释，而是**模型判断该不该调这个
#    工具的唯一依据**。写"查询天气"，模型可能不调；写"查询指定城市未来一天
#    的天气与降水概率"，模型就会调。这是工具设计的第一课。
# ══════════════════════════════════════════════════════════
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市未来一天的天气与降水概率，用于判断是否需要带伞。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "上海"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "查询指定城市的当前本地时间与时区。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "上海"},
                },
                "required": ["city"],
            },
        },
    },
]


# ── 假工具的真实实现 ─────────────────────────────────────
# 注意：模型【不会】执行它们，是你在本地执行，再把结果告诉模型。
def get_weather(city: str) -> dict:
    return {"city": city, "rain": True, "temp_c": 18}


def get_time(city: str) -> dict:
    return {"city": city, "local_time": "2026-09-21T09:00:00+08:00"}


FAKE_IMPL = {"get_weather": get_weather, "get_time": get_time}


def main() -> None:
    user_question = "明天上海要带伞吗？"

    # ══════════════════════════════════════════════════════
    # TODO 2 —— 第一轮调用
    #   构造 payload：{"model": MODEL, "messages": [...], "tools": TOOLS}
    #   messages 目前只有一条 user 消息。
    #   然后把 post(payload) 的返回值赋给 first。
    # ══════════════════════════════════════════════════════
    first: dict = post(
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": user_question,
                }
            ],
            "tools": TOOLS,
        }
    )

    print("\n═══ 第 1 轮原始响应 ═══")
    print(json.dumps(first, ensure_ascii=False, indent=2))
    print("usage:", first.get("usage"))

    # ══════════════════════════════════════════════════════
    # TODO 3 —— 先别往下写。打印出下面三件事，把答案说出来：
    #   a) message["content"] 是什么？message["finish_reason"]（在 choice 层）是什么？
    #   b) type(message["tool_calls"][0]["function"]["arguments"]) 是 str 还是 dict？
    #   c) tool_calls 有几个？它想调哪个工具？参数是什么？
    #
    # ★ 最好的学法：先在纸上写下你的【预期】，再运行对照。
    #   预期和实际不符的地方，就是你今天真正学到的东西。
    # ══════════════════════════════════════════════════════
    msg = first["choices"][0]["message"]

    # ══════════════════════════════════════════════════════
    # TODO 4 —— 执行模型点的那一单，并把结果回灌
    #   要构造 3 条 messages：
    #     [0] 原来的 user 消息
    #     [1] 模型的 assistant 消息 —— 需要【原样放回】，包括 tool_calls
    #         （提示：msg 本身就是个 dict，可以直接用，但要确认字段名对）
    #     [2] 一条 role="tool" 的消息，含：
    #           "tool_call_id" → 对应 tool_calls[0]["id"]
    #           "content"      → 必须是【字符串】，用
    #                            json.dumps(结果, ensure_ascii=False)
    #
    #   别忘了先 json.loads 解析 arguments，再调 FAKE_IMPL 里对应的函数。
    # ══════════════════════════════════════════════════════
    messages: list[dict] = [
        {"role": "user", "content": user_question},
        msg,
    ]

    for call in msg["tool_calls"]:
        fn_name = call["function"]["name"]  # "get_weather"
        args = json.loads(call["function"]["arguments"])  # 字符串 → dict
        if fn_name not in FAKE_IMPL:
            raise ValueError(f"未知工具：{fn_name}")
        result = FAKE_IMPL[fn_name](**args)  # 执行那一单

        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],  # ★ 必须对上
                "content": json.dumps(result, ensure_ascii=False),  # 结果转成字符串
            }
        )

    # ══════════════════════════════════════════════════════
    # TODO 5 —— 第二轮调用
    #   猜一猜：这一轮还要不要带 tools？为什么？
    #   （提示：第二轮如果模型又想调工具呢？）
    # ══════════════════════════════════════════════════════
    second: dict = post({"model": MODEL, "messages": messages, "tools": TOOLS})

    print("\n═══ 第 2 轮原始响应 ═══")
    print(json.dumps(second, ensure_ascii=False, indent=2))
    print("\n最终回答:", second["choices"][0]["message"]["content"])

    # ══════════════════════════════════════════════════════
    # 加餐（做完上面再回来）：把 TODO 4 里回灌的 tool_call_id 故意改错一位，
    # 看看接口报什么错。亲身撞一次，比读十篇文档管用。
    # ══════════════════════════════════════════════════════


if __name__ == "__main__":
    main()
