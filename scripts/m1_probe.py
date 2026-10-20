"""M1 探针 —— 亲眼看清 tool calling 的原始协议。"""

import json

import httpx
from dotenv import dotenv_values

cfg = dotenv_values(".env")
BASE_URL = cfg["LLM_BASE_URL"].rstrip("/")
MODEL = cfg["LLM_MODEL"]
API_KEY = cfg["LLM_API_KEY"]

if not API_KEY:
    raise SystemExit("LLM_API_KEY 还没填，请先编辑 .env")


def post(payload: dict) -> dict:
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json=payload,
        )
        print(f"    [HTTP {resp.status_code}] POST {BASE_URL}/chat/completions")
        if resp.status_code != 200:
            print("    ⚠️ 服务端返回的错误详情：")
            print("    " + resp.text[:800])
        resp.raise_for_status()
        return resp.json()


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


def get_weather(city: str) -> dict:
    return {"city": city, "rain": True, "temp_c": 18}


def get_time(city: str) -> dict:
    return {"city": city, "local_time": "2026-09-21T09:00:00+08:00"}


FAKE_IMPL = {"get_weather": get_weather, "get_time": get_time}


def main() -> None:
    user_question = "明天上海要带伞吗？"

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

    msg = first["choices"][0]["message"]

    messages: list[dict] = [
        {"role": "user", "content": user_question},
        msg,
    ]

    for call in msg["tool_calls"]:
        fn_name = call["function"]["name"]
        args = json.loads(call["function"]["arguments"])
        if fn_name not in FAKE_IMPL:
            raise ValueError(f"未知工具：{fn_name}")
        result = FAKE_IMPL[fn_name](**args)

        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    second: dict = post({"model": MODEL, "messages": messages, "tools": TOOLS})

    print("\n═══ 第 2 轮原始响应 ═══")
    print(json.dumps(second, ensure_ascii=False, indent=2))
    print("\n最终回答:", second["choices"][0]["message"]["content"])


if __name__ == "__main__":
    main()
