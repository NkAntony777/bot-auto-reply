# -*- coding: utf-8 -*-
"""MCP 客户端集成测试：连本地 wxbot_mcp server，走完 run 生命周期 + 预算 + 网关。"""
import asyncio
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8766/mcp"


async def call(session, name, args=None):
    r = await session.call_tool(name, args or {})
    text = "".join(c.text for c in r.content if hasattr(c, "text"))
    return text


async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"=== tools ({len(tools.tools)}) ===")
            print(" ", ", ".join(t.name for t in tools.tools))

            print("\n=== stateless: query_chat_history ===")
            r = await call(session, "query_chat_history",
                           {"conversation": "阿布菠萝", "member": "123", "count": 3})
            print(r[:200])

            print("\n=== run lifecycle: begin_run ===")
            r = await call(session, "begin_run", {"conversation": "阿布菠萝"})
            print(r)
            run_id = json.loads(r)["run_id"]

            print("\n=== budget: send_message x2 (第 2 次应被拦) ===")
            print("1st:", (await call(session, "send_message",
                                     {"run_id": run_id, "text": "MCP 预算测试第一条"}))[:60])
            print("2nd:", (await call(session, "send_message",
                                     {"run_id": run_id, "text": "想发第二条"}))[:60])
            print("filter:", (await call(session, "send_message",
                                         {"run_id": run_id, "text": "API key 泄漏测试"}))[:60])

            print("\n=== antony_call tarot (真实网关) ===")
            r = await call(session, "antony_call",
                           {"run_id": run_id, "tool": "tarot",
                            "params_json": json.dumps({"question": "MCP 链路测试",
                                                       "spreadType": "single"})})
            print(r[:160])

            print("\n=== end_run summary ===")
            print(await call(session, "end_run", {"run_id": run_id}))

            print("\n=== 过期 run_id 应报错 ===")
            print((await call(session, "send_message",
                              {"run_id": run_id, "text": "幽灵调用"}))[:60])


asyncio.run(main())
