# -*- coding: utf-8 -*-
"""wxbot_screener.py — 快筛 gate：用极速小模型先判断「这条消息值不值得回」。

主模型（慢/贵，带人格/记忆/工具）只在快筛放行后才被调用，省掉对无意义消息
（纯表情、无需回应的闲聊、广告链接……）的完整生成。快筛调用失败/解析失败
一律放行（fail-open）——绝不因为快筛丢消息；真正的"不想回"仍可由主模型 [SKIP]。

配置（wxbot_config.json）：

  "screener": {
    "enabled": true,
    "base_url": "",                    # 缺省跟随主 llm 通道
    "model": "step-3.5-flash",         # 极速模型
    "api_key_env": "",                 # 缺省用主通道 key（env → llm.api_key → openclaw）
    "temperature": 0.1,
    "timeout_s": 12,
    "max_ctx_lines": 10,
    "fallbacks": []                    # 快筛自己的备用通道（可选）
  }
"""
import os
import re
import json
import time

SCREENER_SYS = (
    "你是消息筛选器，判断一条新到达的消息是否值得我回复。我是聊天里的一名普通成员"
    "（不是客服，不主动回答没问我的问题）。只输出一个 JSON，格式：\n"
    '{"reply": true, "reason": "一句话理由"}\n'
    "判断标准：\n"
    "- true：被问问题/被@或点名/收到文件图片/聊到与我相关的约定计划/私聊里正常对我说话/"
    "对方明显在等我回应\n"
    "- false：纯表情包或哈哈哈晚安等收尾语/群聊里与我无关的闲聊/广告或链接/无需回应的陈述/"
    "自言自语\n"
    "- 拿不准时倾向 true（宁可多回，不漏回）。reason 用简体中文一句话。"
)


def _post_json(url, payload, api_key, timeout=30):
    """POST JSON（同 wxbot._http_post_json：curl_cffi 优先，退回 urllib）。"""
    try:
        from curl_cffi import requests as creq
        resp = creq.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"},
                         impersonate="chrome", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except ImportError:
        pass
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_key(env_name):
    key = os.environ.get(env_name or "", "")
    if key:
        return key
    try:
        p = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8-sig") as f:
                return (json.load(f).get("env") or {}).get(env_name, "") or ""
    except Exception:
        pass
    return ""


def _channels(cfg):
    """快筛通道链：screener 段没写的字段全部继承主 llm 通道。"""
    sc = cfg.get("screener") or {}
    lc = cfg.get("llm") or {}
    attempts = [{
        "base_url": (sc.get("base_url") or lc.get("base_url") or "").rstrip("/"),
        "model": sc.get("model") or lc.get("model"),
        "_key": (_load_key(sc.get("api_key_env") or lc.get("api_key_env", ""))
                 or sc.get("api_key") or lc.get("api_key") or cfg.get("api_key") or ""),
    }]
    for fb in sc.get("fallbacks", []) or []:
        attempts.append({
            "base_url": (fb.get("base_url") or "").rstrip("/"),
            "model": fb.get("model"),
            "_key": _load_key(fb.get("api_key_env", "")) or fb.get("api_key") or "",
        })
    return attempts, sc


def active(cfg):
    sc = cfg.get("screener") or {}
    return bool(sc.get("enabled", False)) and bool(sc.get("model") or cfg.get("llm", {}).get("model"))


def parse_verdict(text):
    """解析快筛输出 → (reply:bool, reason:str)；解析不出返回 None。"""
    if not text:
        return None
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            flag = obj.get("reply", obj.get("should_reply"))
            if isinstance(flag, bool):
                return flag, str(obj.get("reason", ""))[:60]
            if isinstance(flag, str) and flag.strip().lower() in ("true", "yes", "false", "no"):
                return flag.strip().lower() in ("true", "yes"), str(obj.get("reason", ""))[:60]
        except Exception:
            pass
    # 兜底：裸 token（模型没按格式来时）
    head = text.strip()[:40].lower()
    if re.match(r'^(no|false|skip|不回|无需回复)\b', head):
        return False, "bare-token"
    if re.match(r'^(yes|true|reply|回)\b', head):
        return True, "bare-token"
    return None


def should_reply(cfg, name, incoming, ctx_lines, is_group):
    """快筛入口。返回 (是否回复, 原因)。任何失败都放行（fail-open）。"""
    attempts, sc = _channels(cfg)
    if not attempts or not attempts[0]["_key"]:
        return True, "no-key"
    max_ctx = int(sc.get("max_ctx_lines", 10))
    conv = "\n".join((ctx_lines or [])[-max_ctx:])
    scene = "群聊" if is_group else "私聊"
    user = (
        f"对话类型：{scene}\n\n最近聊天记录（我=自己，其余=别人）：\n{conv or '（无）'}\n\n"
        f"刚收到的这条新消息：\n{incoming}\n\n"
        "这条消息值得我回复吗？只输出 JSON。"
    )
    timeout = int(sc.get("timeout_s", 20))
    for i, a in enumerate(attempts):
        if not a.get("_key") or not a.get("base_url") or not a.get("model"):
            continue
        try:
            payload = {
                "model": a["model"],
                "messages": [{"role": "system", "content": SCREENER_SYS},
                             {"role": "user", "content": user}],
                "temperature": float(sc.get("temperature", 0.1)),
                # 推理型模型会先烧 reasoning tokens 再输出结论，给足余量防 finish=length
                "max_tokens": int(sc.get("max_tokens", 700)),
            }
            data = _post_json(a["base_url"] + "/chat/completions", payload, a["_key"], timeout=timeout)
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            if not text:
                # content 空（推理没烧完/偶发空回）→ 加大力度重试一次
                payload["max_tokens"] = payload["max_tokens"] * 2
                data = _post_json(a["base_url"] + "/chat/completions", payload, a["_key"], timeout=timeout)
                text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            verdict = parse_verdict(text)
            if verdict is not None:
                reply, reason = verdict
                if i > 0:
                    print(f"[screen] fallback ok: {a['model']}")
                return reply, reason
            print(f"[screen] {a['model']} unparseable: {str(text)[:80]!r}")
        except Exception as e:
            print(f"[screen] {a.get('model')} error: {str(e)[:120]}")
            continue
    return True, "fail-open"
