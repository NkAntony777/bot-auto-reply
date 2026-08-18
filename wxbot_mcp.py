# -*- coding: utf-8 -*-
"""wxbot_mcp - bot 能力 MCP server（阶段 B，docs/RESEARCH_AGENT_FRAMEWORK.md）

架构角色：工具层标准化服务。任何 MCP 客户端（ZCode / pi / Claude Desktop /
pydantic 引擎的 MCP toolset）都驱动同一个 bot 身体，同一套预算与审计。

设计决策（评审点 #3 的答案）：
  - **run 会话态**：MCP 本身无状态 per-call，预算（每入站 1 次 send_message、
    tool_budget 次工具调用）必须挂在一个生命周期容器上——`begin_run(conversation)`
    返回 run_id，server 侧维护 run_id→ctx（TTL 30min 自动回收），`end_run`
    返回收口摘要（outbound/img_stem/计数）。预算检查在**工具实现层**，
    换任何入口（builtin/pydantic/MCP/HTTP）都不能绕过。
  - 只读查询工具无状态：直接传 conversation，不需要 run。
  - 直接发送（send_text/send_image）是**操作员工具**：不走 agent 预约，
    但 server 侧限速（默认 60s 间隔）+ 全量走 wxmini2 稳定发送链。
  - 传输：streamable-http 绑 127.0.0.1（stdout 留给日志；与 wxapi 同信任模型）。

运行：python wxbot_mcp.py   →  http://127.0.0.1:8766/mcp
客户端配置（ZCode 用户级 mcp 设置）：
  {"mcpServers": {"wxbot": {"type": "http", "url": "http://127.0.0.1:8766/mcp"}}}
"""
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import wxbot
import wxbot_agent as core
import wxmini2 as wx

mcp = FastMCP("wxbot", instructions=(
    "微信 bot 阿廖沙的能力层。只读查询直接传 conversation（群名/昵称，支持子串）；"
    "需要预算的操作（send_message 预约发送 / generate_image / antony_call 玄学工具）"
    "先 begin_run 拿 run_id，用完 end_run 取收口摘要。"
    "send_text/send_image 是操作员直发工具（60s 限速），慎用。"))

DEFAULT_MCP_CFG = {
    "enabled": True,
    "host": "127.0.0.1",
    "port": 8766,
    "run_ttl_s": 1800,
    "direct_send_gap_s": 60.0,
}

_RUNS = {}                      # run_id -> {"ctx": dict, "ts": float}
_LAST_DIRECT_SEND = [0.0]


def _mcfg():
    out = dict(DEFAULT_MCP_CFG)
    out.update(wxbot.load_config().get("mcp") or {})
    return out


def _gc_runs():
    ttl = _mcfg()["run_ttl_s"]
    now = time.time()
    for rid in [k for k, v in _RUNS.items() if now - v["ts"] > ttl]:
        print(f"[mcp] run {rid[:8]} expired (outbound={_RUNS[rid]['ctx'].get('outbound') is not None})")
        del _RUNS[rid]


def _resolve_conversation(name: str):
    """会话名（子串匹配）→ (全名, username)。找不到抛 ValueError。"""
    for s in wx.db_sessions(limit=50):
        if name and name in s["name"]:
            return s["name"], s["username"]
    raise ValueError(f"找不到会话「{name}」，可用 list_sessions 查看最近会话")


def _ctx_for_run(run_id: str) -> dict:
    _gc_runs()
    run = _RUNS.get(run_id)
    if not run:
        raise ValueError(f"run_id 无效或已过期：{run_id[:8]}（先 begin_run）")
    run["ts"] = time.time()
    return run["ctx"]


def _budget_check(ctx: dict):
    """工具预算（与 builtin/_budgeted 同语义）。超限返回回填文本或 None。"""
    acfg = core._acfg(ctx["cfg"])
    if ctx.get("tool_count", 0) >= int(acfg["tool_budget"]):
        return "工具额度已用完，请直接给最终回复。"
    ctx["tool_count"] = ctx.get("tool_count", 0) + 1
    return None


def _trunc(text: str) -> str:
    limit = int(core._acfg(wxbot.load_config())["result_max_chars"])
    text = str(text)
    return text[:limit] + ("\n…(结果过长已截断)" if len(text) > limit else "")


def _direct_send_gate() -> str | None:
    """操作员直发限速：距上次直发不足 gap 则拒绝（返回错误文本）。"""
    gap = _mcfg()["direct_send_gap_s"]
    wait = _LAST_DIRECT_SEND[0] + gap - time.time()
    if wait > 0:
        return f"直发限速中，{wait:.0f}s 后再试（agent 预约发送不受此限，走 send_message）"
    _LAST_DIRECT_SEND[0] = time.time()
    return None


# ================================================================ run 生命周期

@mcp.tool()
def begin_run(conversation: str) -> str:
    """开始一个 agent run（预算生命周期容器）。
    conversation=群名/联系人昵称（子串匹配）。返回 run_id——预算类工具都需要它。"""
    cfg = wxbot.load_config()
    name, username = _resolve_conversation(conversation)
    run_id = uuid.uuid4().hex[:12]
    ctx = core.new_ctx(cfg, name, username, "", True)
    _RUNS[run_id] = {"ctx": ctx, "ts": time.time()}
    print(f"[mcp] begin_run {run_id} -> {name}")
    return json.dumps({"run_id": run_id, "conversation": name,
                       "username": username, "tool_budget": core._acfg(cfg)["tool_budget"]},
                      ensure_ascii=False)


@mcp.tool()
def end_run(run_id: str) -> str:
    """结束 run 并返回收口摘要：outbound（预约发送的最终文本）、img_stem（生成图标记）、
    计数。调用方（人或框架）应把 outbound 作为要发的内容、img_stem 组成 [IMG:stem] 行。"""
    ctx = _ctx_for_run(run_id)
    summary = {
        "outbound": ctx.get("outbound"),
        "img_stem": ctx.get("img_stem"),
        "sent_count": ctx.get("sent_count", 0),
        "tool_count": ctx.get("tool_count", 0),
        "img_marker": f"[IMG:{ctx['img_stem']}]" if ctx.get("img_stem") else None,
    }
    del _RUNS[run_id]
    print(f"[mcp] end_run {run_id[:8]}: {summary['tool_count']} tools, "
          f"outbound={'yes' if summary['outbound'] else 'no'}")
    return json.dumps(summary, ensure_ascii=False)


# ================================================================ 只读查询（无状态）

@mcp.tool()
def query_chat_history(conversation: str, member: str = "", count: int = 5) -> str:
    """查会话里某成员最近的发言（member 留空=返回会话最新几条）。count 1-20。"""
    name, username = _resolve_conversation(conversation)
    ctx = core.new_ctx(wxbot.load_config(), name, username, "", True)
    return _trunc(core._mk_query_chat_history(ctx)({"member": member, "count": count}))


@mcp.tool()
def query_member_info(conversation: str, nickname: str) -> str:
    """查成员信息：发言次数、最后活跃时间、近期发言摘录。"""
    name, username = _resolve_conversation(conversation)
    ctx = core.new_ctx(wxbot.load_config(), name, username, "", True)
    return _trunc(core._mk_query_member_info(ctx)({"nickname": nickname}))


@mcp.tool()
def query_group_stats(conversation: str, days: int = 7) -> str:
    """统计群里最近 N 天（1-30）发言活跃度：每人条数+最后活跃。"""
    name, username = _resolve_conversation(conversation)
    ctx = core.new_ctx(wxbot.load_config(), name, username, "", True)
    return _trunc(core._mk_query_group_stats(ctx)({"days": days}))


@mcp.tool()
def list_sessions(limit: int = 20) -> str:
    """列出最近的微信会话（调试/找群名用）。"""
    rows = [{"name": s["name"], "username": s["username"],
             "last": (s.get("last") or "")[:40]} for s in wx.db_sessions(limit=limit)]
    return json.dumps(rows, ensure_ascii=False, indent=1)


# ================================================================ antony.best 扩展能力（wxbot_hub）

@mcp.tool()
def search_books(query: str, page: int = 1) -> str:
    """搜书（Anna's Archive，经 antony.best 代理）：按书名/作者/ISBN 找电子书资源。"""
    import wxbot_hub
    return _trunc(wxbot_hub.search_books(wxbot.load_config(), query, page))


@mcp.tool()
def search_videos(query: str) -> str:
    """搜影视资源（光影阁聚合源，经 antony.best 代理；需配置 hub.cine_gate_answer）。"""
    import wxbot_hub
    return _trunc(wxbot_hub.search_videos(wxbot.load_config(), query))


@mcp.tool()
def kb_mangpai(query: str, type: str = "all") -> str:
    """盲派命理知识库检索（2067 条结构化条目）：查盲派技法/口诀/案例，含质量与来源标注。"""
    import wxbot_hub
    return _trunc(wxbot_hub.kb_mangpai(wxbot.load_config(), query, type))


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索（AnySearch 全网搜，经 antony.best 代理；复用 PrivateGate 密语）。"""
    import wxbot_hub
    return _trunc(wxbot_hub.web_search(wxbot.load_config(), query, max_results))


@mcp.tool()
def web_extract(url: str) -> str:
    """抓取指定 URL 的网页正文（搜索结果需要看详情时用）。"""
    import wxbot_hub
    return _trunc(wxbot_hub.web_extract(wxbot.load_config(), url))


@mcp.tool()
def speak(text: str) -> str:
    """TTS 合成一段阿廖沙音色（正太少年音）的语音并发到会话（操作员直发，限速内）。"""
    import wxbot_tts
    cfg = wxbot.load_config()
    try:
        path, _stem = wxbot_tts.synthesize(cfg, text)
    except Exception as e:
        return f"合成失败：{e}"
    import wxmini2 as wxm
    name = "阿布菠萝"
    for s in wxm.db_sessions(limit=30):
        if "阿布菠萝" in s["name"]:
            name = s["name"]
            break
    ok = wxm.send_file(name, path)
    return "已发送语音（DB 确认）" if ok else "发送失败（见 wxmini2 日志）"


# ================================================================ 预算类（需 run_id）

@mcp.tool()
def send_message(run_id: str, text: str) -> str:
    """预约发送一条消息（agent 语义）：每 run 限 1 次，内容过滤+截断。
    真正的发送由调用方在 end_run 后按 outbound 执行（bot 场景走 poll 发送链）。"""
    ctx = _ctx_for_run(run_id)
    hit = _budget_check(ctx)
    if hit:
        return hit
    return _trunc(core._mk_send_message(ctx)({"text": text}))


@mcp.tool()
def generate_image(run_id: str, prompt: str) -> str:
    """StepFun 文生图（每 run 计入工具预算）。成功后 img_stem 记录在 run 里，
    end_run 的摘要给出 [IMG:stem] 标记。"""
    ctx = _ctx_for_run(run_id)
    hit = _budget_check(ctx)
    if hit:
        return hit
    return _trunc(core._mk_generate_image(ctx)({"prompt": prompt}))


@mcp.tool()
def antony_call(run_id: str, tool: str, params_json: str = "{}") -> str:
    """调用 antony.best 玄学工具（bazi/tarot/liuyao/almanac 等 13 个，见 antony_catalog）。
    params_json=参数 JSON 字符串；seed 未给时按 run 的消息指纹自动注入（同问同答）。"""
    ctx = _ctx_for_run(run_id)
    hit = _budget_check(ctx)
    if hit:
        return hit
    try:
        params = json.loads(params_json) if params_json else {}
        assert isinstance(params, dict)
    except Exception:
        return "params_json 不是合法 JSON 对象"
    return _trunc(core._exec_gateway(ctx["cfg"], ctx, tool, params))


@mcp.tool()
def antony_catalog() -> str:
    """antony.best 工具目录（名称/描述/参数 schema）。"""
    return _trunc(json.dumps(core._gateway(wxbot.load_config()).catalog(), ensure_ascii=False, indent=1))


# ================================================================ 操作员直发（限速）

@mcp.tool()
def send_text(conversation: str, text: str) -> str:
    """【操作员直发】直接把文本发到会话（走完整视觉发送链，等键鼠空闲）。
    与 agent 无关、不占预算，但限速 60s/次。自动化场景请改用 begin_run+send_message。"""
    blocked = _direct_send_gate()
    if blocked:
        return blocked
    name, _ = _resolve_conversation(conversation)
    ok = wx.send_text(name, text[:500])
    return "已发送（DB 确认）" if ok else "发送失败（见 wxmini2 日志；草稿保留未删除）"


@mcp.tool()
def send_image(conversation: str, path: str) -> str:
    """【操作员直发】发送本地图片文件到会话（剪贴板粘贴链，60s 限速）。"""
    blocked = _direct_send_gate()
    if blocked:
        return blocked
    name, _ = _resolve_conversation(conversation)
    ok = wx.send_image(name, path)
    return "已发送（DB 确认图片消息）" if ok else "发送失败（见 wxmini2 日志）"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    c = _mcfg()
    if not c["enabled"]:
        print("wxbot_mcp disabled in config")
        sys.exit(0)
    print(f"wxbot_mcp serving on http://{c['host']}:{c['port']}/mcp "
          f"(run_ttl={c['run_ttl_s']}s, direct_send_gap={c['direct_send_gap_s']}s)")
    mcp.settings.host = c["host"]
    mcp.settings.port = c["port"]
    mcp.run(transport="streamable-http")
