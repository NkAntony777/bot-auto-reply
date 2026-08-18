# -*- coding: utf-8 -*-
"""wxbot_agent - Phase 1 tool-calling agent 循环（任务书 docs/TASK_PHASE1_AGENT_LOOP.md）

混合路由 reply_dispatch：
  快路径 = 原 wxbot.llm_reply（无工具信号，延迟体验不变）
  慢路径 = agent_reply（OpenAI tools 多轮循环，max_rounds / tool_budget 双预算）

工具：
  内部 4 个：query_chat_history / query_member_info / query_group_stats / send_message
  外部：antony.best 网关玄学工具（wxbot_gateway.Gateway.llm_tools 按相关性注入）

安全（T4）：
  - 每条入站消息 send_message 至多安排 1 次（超出回错误文本让模型改口）
  - send_message 正文截断 max_reply_chars + 系统词过滤（系统/配置/API/token）
  - 工具结果截断 result_max_chars（bazi 排盘 Markdown 可达 4-6KB）
  - 工具异常只回填错误文本；整链失败返回 None（poll 侧退避接管）

发送语义（坑 #5）：send_message 是"预约发送"——真正的发送仍走 poll 现有
delay + split_sentences + wx.send_text 稳定链路，agent 循环内不碰窗口不 sleep。
"""
import hashlib
import json
import re
import time

import wxbot
import wxmini2 as wx
from wxbot_gateway import Gateway

DEFAULT_AGENT_CFG = {
    "enabled": True,
    "max_rounds": 5,
    "tool_budget": 8,
    "result_max_chars": 2000,
    "allow_send_message": True,
    "route_keywords": ["查", "谁", "最近", "排盘", "占卜", "算一卦", "塔罗", "黄历", "八字", "紫微", "六爻"],
}

# 点名+问句路由的问句信号（中文无空格，只能子串匹配）
_QUESTION_HINTS = ("？", "?", "吗", "什么", "怎么", "为什么", "多少", "几", "哪", "帮我")

# send_message 内容过滤：疑似系统/密钥泄露直接拒绝（任务书 T4）
_SEND_FORBIDDEN = re.compile(r"(系统|配置|密钥|api[_ -]?key|token)", re.I)

_GW = [None]  # Gateway 单例（目录缓存跨轮复用）


def _gateway(cfg):
    if _GW[0] is None:
        _GW[0] = Gateway(cfg)
    return _GW[0]


def _acfg(cfg):
    out = dict(DEFAULT_AGENT_CFG)
    out.update(cfg.get("agent") or {})
    return out


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(结果过长已截断)"


# ================================================================ 内部工具

def _mk_query_chat_history(ctx):
    def exec_fn(args):
        member = (args.get("member") or "").strip()
        try:
            count = max(1, min(20, int(args.get("count", 5))))
        except (TypeError, ValueError):
            count = 5
        if not ctx.get("username"):
            return "当前会话无法定位数据库记录，查不了历史。"
        msgs = wx.read_chat_db(ctx["username"], limit=50)
        senders = []
        for m in msgs:
            s = m.get("sender") or ""
            if s and s not in ("我", "对方") and s not in senders:
                senders.append(s)
        if not member:
            recent = msgs[-count:]
            lines = [_fmt_msg(m) for m in recent]
            return f"本会话最新 {len(lines)} 条（成员：{'、'.join(senders[:15])}）：\n" + "\n".join(lines)
        hits = [m for m in msgs if member in (m.get("sender") or "")]
        if not hits:
            return (f"最近 50 条里没找到「{member}」的发言。"
                    f"最近发言的人有：{'、'.join(senders[:15]) or '（无）'}")
        shown = hits[-count:]
        lines = [_fmt_msg(m) for m in shown]
        return (f"「{member}」最近 50 条中共 {len(hits)} 条发言，"
                f"以下是最新 {len(shown)} 条：\n" + "\n".join(lines))
    return exec_fn


def _fmt_msg(m):
    t = time.strftime("%m-%d %H:%M", time.localtime(m.get("ts") or 0))
    kind = "" if m.get("kind") == "text" else f"[{m.get('kind')}]"
    return f"[{t}] {m.get('sender') or '?'}: {kind}{(m.get('text') or '')[:80]}"


def _mk_query_member_info(ctx):
    def exec_fn(args):
        nick = (args.get("nickname") or "").strip()
        if not nick:
            return "请给出要查询的成员昵称。"
        if not ctx.get("username"):
            return "当前会话无法定位数据库记录。"
        msgs = wx.read_chat_db(ctx["username"], limit=200)
        hits = [m for m in msgs if nick in (m.get("sender") or "")]
        lines = [f"成员「{nick}」："]
        if hits:
            last = hits[-1]
            t = time.strftime("%m-%d %H:%M", time.localtime(last.get("ts") or 0))
            lines.append(f"最近 200 条中发言 {len(hits)} 条，最后活跃 {t}")
            for m in hits[-3:]:
                lines.append("摘录 " + _fmt_msg(m))
        else:
            lines.append("最近 200 条里没有该成员的发言（可能是潜水党或昵称不对）。")
        try:
            u = wx._name_to_username(nick)
            if u:
                lines.append(f"通讯录匹配：{u}")
        except Exception:
            pass
        return "\n".join(lines)
    return exec_fn


def _mk_query_group_stats(ctx):
    def exec_fn(args):
        try:
            days = max(1, min(30, int(args.get("days", 7))))
        except (TypeError, ValueError):
            days = 7
        if not ctx.get("username"):
            return "当前会话无法定位数据库记录。"
        cutoff = time.time() - days * 86400
        msgs = [m for m in wx.read_chat_db(ctx["username"], limit=500)
                if (m.get("ts") or 0) >= cutoff]
        stats = {}
        for m in msgs:
            s = m.get("sender") or "未知"
            if s in ("我",):
                s = "阿廖沙(我)"
            st = stats.setdefault(s, {"n": 0, "last": 0})
            st["n"] += 1
            st["last"] = max(st["last"], m.get("ts") or 0)
        if not stats:
            return f"最近 {days} 天本会话没有可统计的消息。"
        rows = sorted(stats.items(), key=lambda kv: -kv[1]["n"])[:15]
        lines = [f"最近 {days} 天发言统计（覆盖最新 {len(msgs)} 条）："]
        for s, st in rows:
            t = time.strftime("%m-%d %H:%M", time.localtime(st["last"]))
            lines.append(f"  {s}: {st['n']} 条，最后活跃 {t}")
        return "\n".join(lines)
    return exec_fn


def _mk_send_message(ctx):
    """预约发送：记录到 ctx['outbound']，由 poll 的稳定发送链路真正发出。"""
    def exec_fn(args):
        text = (args.get("text") or "").strip()
        acfg = ctx["acfg"]
        if not acfg.get("allow_send_message", True):
            return "发送功能当前已关闭，请直接给出最终文字回复。"
        if not text:
            return "text 不能为空。"
        if ctx["sent_count"] >= 1:
            return ("发送额度已用完：每条入站消息只能主动发 1 条。"
                    "请把补充内容并入已安排的发送，或直接输出最终回复。")
        if _SEND_FORBIDDEN.search(text):
            return "内容包含敏感系统词汇，已拒绝发送。请换成人话，不要谈论系统/配置/密钥。"
        limit = int(ctx["cfg"]["reply"].get("max_reply_chars", 300))
        if len(text) > limit:
            text = text[:limit]
            print(f"[agent] send_message truncated to {limit} chars")
        ctx["outbound"] = text
        ctx["sent_count"] += 1
        return (f"已安排发送（{len(text)} 字）。这就是你的最终回复内容，"
                "之后只需简短收尾或直接结束，不要再安排别的发送。")
    return exec_fn


_INTERNAL_TOOLS = [
    ("query_chat_history",
     "查询当前会话里某个成员最近的发言记录；不填 member 则返回会话最新几条。",
     {"type": "object",
      "properties": {"member": {"type": "string", "description": "成员昵称（支持部分匹配）"},
                     "count": {"type": "integer", "description": "返回条数 1-20，默认 5"}},
      "required": ["member"]},
     _mk_query_chat_history),
    ("query_member_info",
     "查询群成员信息：发言次数、最后活跃时间、近期发言摘录。",
     {"type": "object",
      "properties": {"nickname": {"type": "string", "description": "成员昵称"}},
      "required": ["nickname"]},
     _mk_query_member_info),
    ("query_group_stats",
     "统计群里最近 N 天的发言活跃度：每人发言条数与最后活跃时间。",
     {"type": "object",
      "properties": {"days": {"type": "integer", "description": "统计天数 1-30，默认 7"}}},
     _mk_query_group_stats),
    ("send_message",
     "把你最终要说的话作为一条消息发出（整段发出，可含换行分句）。每条入站消息只能调用一次。",
     {"type": "object",
      "properties": {"text": {"type": "string", "description": "要发送的完整文本"}},
      "required": ["text"]},
     _mk_send_message),
]


def build_toolset(cfg, ctx):
    """按本次调用上下文构建工具集：name -> {spec, exec}（网关工具 exec=None 走网关分发）。"""
    tools = {}
    allow_send = ctx["acfg"].get("allow_send_message", True)
    for name, desc, params, factory in _INTERNAL_TOOLS:
        if name == "send_message" and not allow_send:
            continue
        tools[name] = {
            "spec": {"type": "function",
                     "function": {"name": name, "description": desc, "parameters": params}},
            "exec": factory(ctx),
        }
    try:
        for t in _gateway(cfg).llm_tools(ctx.get("inbound") or ""):
            tools[t["function"]["name"]] = {"spec": t, "exec": None}
    except Exception as e:
        print(f"[agent] gateway tools unavailable: {e}")
    return tools


def _tool_guide(tools):
    lines = ["【本轮可用工具】"]
    for name, t in tools.items():
        desc = (t["spec"]["function"]["description"] or "").split("。")[0]
        lines.append(f"- {name}：{desc}")
    lines.append(
        "规则：涉及事实查证、历史记录、统计、排盘占卜时先调工具拿素材再回复，日常闲聊不要调；"
        "工具结果只是素材，最终回复保持你的人格语气、简短自然，别照搬工具的格式化报告，"
        "也别提\"工具/函数/API\"这些词。最后直接输出要发送的微信文本。")
    return "\n".join(lines)


# ================================================================ 工具执行

def _exec_gateway(cfg, ctx, name, args):
    gw = _gateway(cfg)
    params = dict(args)
    # seed 注入：工具声明了 seed 且模型没给时，用消息指纹定死（同消息复现同结果）
    try:
        spec = {t["name"]: t for t in gw.catalog()}.get(name.removeprefix("antony_")) or {}
        props = ((spec.get("parameters") or {}).get("properties") or {})
        if "seed" in props and "seed" not in params:
            params["seed"] = hashlib.md5(
                f"{ctx.get('conversation')}|{ctx.get('inbound')}".encode("utf-8")).hexdigest()[:16]
    except Exception:
        pass
    r = gw.call(name, params)
    if r.get("ok"):
        return r.get("text") or "(工具返回空结果)"
    return (f"工具 {name} 暂时不可用：{r.get('error', '未知错误')[:120]}。"
            "请自然告知用户这会儿算不了，稍后再试，不要编造结果。")


def _exec_tool(tools, cfg, ctx, name, args):
    t = tools.get(name)
    if t is not None and t["exec"] is not None:
        return t["exec"](args)
    if name.startswith("antony_"):
        return _exec_gateway(cfg, ctx, name, args)
    return f"未知工具：{name}。请只调用本轮可用工具列表里的。"


# ================================================================ LLM 调用（tools 透传 + fallback 链）

def _extract_text(msg):
    c = msg.get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        text = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    else:
        text = ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return (text or "").strip()


def _llm_chat(cfg, messages, tools=None):
    """带 tools 的 chat completions（主链挂了走 fallbacks）。返回 (message, finish_reason)。
    全链失败返回 (None, None)。MiniMax m3 与 StepFun 均为标准 OpenAI tool_calls 格式（已实测）。"""
    lcfg = cfg["llm"]
    attempts = [{
        "base_url": lcfg["base_url"],
        "model": lcfg["model"],
        "_key": wxbot._api_key(cfg),
    }]
    for fb in lcfg.get("fallbacks", []) or []:
        attempts.append({
            "base_url": fb["base_url"],
            "model": fb["model"],
            "_key": fb.get("api_key") or wxbot._load_api_key(fb.get("api_key_env", "")),
        })
    for i, a in enumerate(attempts):
        if not a["_key"]:
            continue
        url = a["base_url"].rstrip("/") + "/chat/completions"
        payload = {
            "model": a["model"],
            "messages": messages,
            "temperature": lcfg.get("temperature", 0.9),
            "max_tokens": lcfg.get("max_tokens", 2400),
        }
        _thinking = lcfg.get("thinking")
        if _thinking is not None:
            payload["thinking"] = _thinking
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            data = wxbot._http_post_json(url, payload, a["_key"], timeout=120)
            ch = (data.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            if not (msg.get("tool_calls") or _extract_text(msg)):
                # 思考型模型可能首轮烧完 token：原样重试一次
                data = wxbot._http_post_json(url, payload, a["_key"], timeout=120)
                ch = (data.get("choices") or [{}])[0]
                msg = ch.get("message") or {}
                if not (msg.get("tool_calls") or _extract_text(msg)):
                    raise ValueError("empty LLM response")
            if i > 0:
                print(f"[agent] llm fallback ok: {a['model']}")
            return msg, ch.get("finish_reason")
        except Exception as e:
            if "invalid temperature" in str(e).lower():
                retry_payload = dict(payload)
                retry_payload.pop("temperature", None)
                try:
                    data = wxbot._http_post_json(url, retry_payload, a["_key"], timeout=120)
                    ch = (data.get("choices") or [{}])[0]
                    msg = ch.get("message") or {}
                    if msg.get("tool_calls") or _extract_text(msg):
                        return msg, ch.get("finish_reason")
                except Exception as retry_error:
                    print(f"[agent] retry w/o temperature error ({a['model']}):", retry_error)
            print(f"[agent] llm error ({a['model']}):", e)
    return None, None


# ================================================================ agent 循环（T2）

def agent_reply(cfg, conversation, inbound, ctx_lines=None, is_group=True,
                username=None, max_rounds=None, tool_budget=None):
    acfg = _acfg(cfg)
    max_rounds = int(max_rounds or acfg["max_rounds"])
    tool_budget = int(tool_budget or acfg["tool_budget"])
    result_max = int(acfg["result_max_chars"])
    ctx = {
        "cfg": cfg, "acfg": acfg, "conversation": conversation,
        "username": username, "inbound": inbound, "is_group": is_group,
        "outbound": None, "sent_count": 0,
    }

    system = wxbot.system_prompt_for(cfg, conversation, inbound, is_group)
    tools = build_toolset(cfg, ctx)
    guide = _tool_guide(tools)

    if ctx_lines:
        ctx_str = "\n".join(ctx_lines)
        pname = wxbot.persona_for_conversation(cfg, conversation, is_group)
        style_block = wxbot.style_learning_block(cfg, pname, ctx_lines, is_group)
        user_content = (
            f"这是「{conversation}」里最近的聊天记录（我=张宇轩这边发的，对方=别人发的）：\n{ctx_str}\n\n"
            f"{style_block}\n"
            "先在心里判断当前话题、对方意图和语气。"
            f"请针对最后一条对方消息，以主人朋友的身份自然回复：\n{inbound}\n\n{guide}"
        )
    else:
        user_content = f"这是{conversation}里的新消息，请以主人朋友的身份自然回复：\n{inbound}\n\n{guide}"

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    tool_array = [t["spec"] for t in tools.values()]
    budget = tool_budget
    final_text = None
    t0 = time.time()

    for rnd in range(1, max_rounds + 1):
        # 最后一轮不再给工具，逼模型收口；预算耗尽同理
        use_tools = budget > 0 and rnd < max_rounds
        msg, _finish = _llm_chat(cfg, messages, tool_array if use_tools else None)
        if msg is None:
            if rnd == 1:
                return None  # 全链失败，poll 侧退避接管
            break  # 中途失败：不再硬试，用已积累的信息收口
        tcs = msg.get("tool_calls") or []
        if not tcs:
            final_text = _extract_text(msg)
            break
        # ---- 执行工具调用 ----
        messages.append({"role": "assistant",
                         "content": msg.get("content") or "", "tool_calls": tcs})
        if not use_tools:
            for j, tc in enumerate(tcs):
                tc_id = tc.get("id") or f"call_{rnd}_{j}"
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                 "content": "已达轮数/额度上限，请直接给最终文字回复，不要再调用工具。"})
            msg2, _ = _llm_chat(cfg, messages, None)
            if msg2 is not None:
                final_text = _extract_text(msg2)
            break
        for j, tc in enumerate(tcs):
            tc_id = tc.get("id") or f"call_{rnd}_{j}"
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            raw = fn.get("arguments")
            try:
                args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception:
                args = None
            if not isinstance(args, dict):
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                 "content": "arguments 不是合法 JSON 对象，请重试并传标准 JSON。"})
                continue
            if budget <= 0:
                messages.append({"role": "tool", "tool_call_id": tc_id,
                                 "content": "工具额度已用完，请直接给最终回复。"})
                continue
            budget -= 1
            try:
                result = _exec_tool(tools, cfg, ctx, name, args)
            except Exception as e:
                result = f"工具执行出错：{e}"
            result = _truncate(str(result), result_max)
            print(f"[agent] round {rnd} tool={name}"
                  f"({json.dumps(args, ensure_ascii=False)[:80]}) -> {len(result)}字 "
                  f"({time.time() - t0:.1f}s)")
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": result})
        # 继续下一轮

    if final_text is None:
        if ctx["outbound"] is not None:
            print(f"[agent] no final text, using outbound ({len(ctx['outbound'])} chars)")
            return ctx["outbound"]
        print(f"[agent] rounds exhausted without final text ({time.time() - t0:.1f}s)")
        return "[SKIP]"  # 跑完轮数仍无文本：按跳过处理，不触发 poll 退避

    if ctx["outbound"] is not None:
        if final_text and final_text != ctx["outbound"]:
            print(f"[agent] outbound overrides final text "
                  f"({len(ctx['outbound'])} vs {len(final_text)} chars)")
        return ctx["outbound"]

    limit = int(cfg["reply"].get("max_reply_chars", 300))
    print(f"[agent] done in {time.time() - t0:.1f}s, {tool_budget - budget} tool calls")
    return final_text[:limit] if final_text else None


# ================================================================ 混合路由（T3）

def _route_reason(cfg, inbound, is_group):
    """慢路径触发判定：None=快路径。"""
    acfg = _acfg(cfg)
    if not acfg.get("enabled", True):
        return None
    text = inbound or ""
    # a) 网关工具相关性命中
    try:
        if _gateway(cfg).relevance(text):
            return "gateway"
    except Exception:
        pass
    # b) 路由关键词
    for kw in acfg.get("route_keywords", []):
        if kw and kw in text:
            return f"kw:{kw}"
    # c) 群内被点名 + 问句信号
    if is_group:
        names = (["阿廖沙"] + (cfg["reply"]["group"].get("mention_names") or [])
                 + (cfg.get("own_nicknames") or []))
        names = [n for n in names if n and n != "YOUR_WECHAT_NICKNAME"]
        if any(n in text for n in names) and any(q in text for q in _QUESTION_HINTS):
            return "mention+question"
    return None


def reply_dispatch(cfg, conversation, inbound, ctx_lines=None, is_group=True, username=None):
    """poll 唯一入口：无信号走原 llm_reply（快路径），有信号走 agent_reply（慢路径）。"""
    reason = _route_reason(cfg, inbound, is_group)
    if not reason:
        return wxbot.llm_reply(cfg, conversation, inbound, context=ctx_lines, is_group=is_group)
    print(f"[agent] slow path ({reason}) -> {conversation}")
    try:
        return agent_reply(cfg, conversation, inbound, ctx_lines=ctx_lines,
                           is_group=is_group, username=username)
    except Exception as e:
        print(f"[agent] loop crashed, fallback to fast path: {e}")
        return wxbot.llm_reply(cfg, conversation, inbound, context=ctx_lines, is_group=is_group)


# ================================================================ 自测入口

if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else "route"
    cfg = wxbot.load_config()

    if mode == "route":
        # 快/慢路径判定表（不发消息不调 LLM）
        samples = [
            ("喵喵喵", True), ("哈哈哈哈", True),
            ("52 最近都聊了什么", True),
            ("帮我排个八字：2024-06-15 14:30 女", True),
            ("塔罗测下明天运势", True),
            ("今天黄历宜什么", True),
            ("阿廖沙 你叫什么名字呀？", True),
        ]
        for s, g in samples:
            print(f"  {s[:24]:26s} -> {_route_reason(cfg, s, g) or 'FAST'}")

    elif mode == "ask":
        # 完整 agent_reply（真实调 LLM/网关/DB，但不发送消息）
        inbound = sys.argv[2] if len(sys.argv) > 2 else "帮我排个八字：2024-06-15 14:30 女"
        target = None
        for s in wx.db_sessions(limit=30):
            if "阿布菠萝" in s["name"]:
                target = s
                break
        if not target:
            print("找不到目标群")
            sys.exit(1)
        msgs = wx.read_chat_db(target["username"], limit=12)
        ctx_lines = []
        for m in msgs:
            who = "我" if m["side"] == "own" else (m.get("sender") or "对方")
            ctx_lines.append(f"{who}: {m['text'][:100]}")
        print(f"=== ask [{target['name']}] {inbound!r} ===")
        reply = reply_dispatch(cfg, target["name"],
                               f"【发送者昵称: 52】【普通群友: 必须礼貌友善、积极帮助】{inbound}",
                               ctx_lines=ctx_lines, is_group=True,
                               username=target["username"])
        print(f"--- reply ---\n{reply}")

    elif mode == "sendbudget":
        # send_message 预算单测（不发送）
        ctx = {"cfg": cfg, "acfg": _acfg(cfg), "conversation": "test",
               "username": None, "inbound": "x", "is_group": True,
               "outbound": None, "sent_count": 0}
        fn = _mk_send_message(ctx)
        print("1st:", fn({"text": "正常一条"}))
        print("2nd:", fn({"text": "想再发一条"})[:40], "...")
        print("filter:", fn({"text": "我的API key是xxx"}))
        print("outbound =", ctx["outbound"])
