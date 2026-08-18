# -*- coding: utf-8 -*-
"""wxbot_agent_py - PydanticAI v2 引擎（阶段 A，docs/RESEARCH_AGENT_FRAMEWORK.md）

与 builtin（wxbot_agent）同语义，框架只负责"脑"的部分：
  - 工具参数 typed 校验（替代裸 json.loads）
  - FallbackModel 表达 StepFun → MiniMax 链（替代手写循环 fallback）
  - output validator + retries 处理 StepFun 空响应（思考烧完 token）
  - UsageLimits 做轮数/工具数兜底

产品语义全部留在框架外：
  - 预算回填："工具额度用完请收口"在工具包装层实现（先于框架 limit 触发）
  - send_message 预约 / [IMG:] 兜底 / 截断：走 wxbot_agent.finalize_reply（双引擎共用）
  - 快路径不进框架（路由在 wxbot_agent.reply_dispatch 共用）
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict

from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import AgentRunError, ModelRetry
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits

import wxbot
import wxbot_agent as core

_AGENT_CACHE: Dict[str, tuple] = {}   # 配置指纹 -> (Agent, 指纹内容)


@dataclass
class Deps:
    """每 run 状态：直接复用 core 的 ctx dict（outbound/sent_count/img_stem/tool_count）。"""
    ctx: dict
    budget: int = 8
    result_max: int = 2000
    extra_lines: list = field(default_factory=list)   # 工具执行日志（收口时打印）


# ================================================================ 工具包装
# typed 签名做参数校验，实现转发给 core 的工厂（零逻辑重复）；
# docstring = 工具描述（pydantic-ai 从 docstring 生成），与 builtin 注册表同文案

def _q_history(rc: RunContext[Deps], member: str, count: int = 5) -> str:
    """查询当前会话里某个成员最近的发言记录；不填 member 则返回会话最新几条。"""
    return core._mk_query_chat_history(rc.deps.ctx)({"member": member, "count": count})


def _q_member(rc: RunContext[Deps], nickname: str) -> str:
    """查询群成员信息：发言次数、最后活跃时间、近期发言摘录。"""
    return core._mk_query_member_info(rc.deps.ctx)({"nickname": nickname})


def _q_stats(rc: RunContext[Deps], days: int = 7) -> str:
    """统计群里最近 N 天的发言活跃度：每人发言条数与最后活跃时间。"""
    return core._mk_query_group_stats(rc.deps.ctx)({"days": days})


def _send_message(rc: RunContext[Deps], text: str) -> str:
    """把你最终要说的话作为一条消息发出（整段发出，可含换行分句）。每条入站消息只能调用一次。"""
    return core._mk_send_message(rc.deps.ctx)({"text": text})


def _generate_image(rc: RunContext[Deps], prompt: str) -> str:
    """AI 画图：按文字描述生成一张图片并发到当前会话。用户想看图/让你画什么时用。"""
    return core._mk_generate_image(rc.deps.ctx)({"prompt": prompt})


def _search_books(rc: RunContext[Deps], query: str, page: int = 1) -> str:
    """搜书（Anna's Archive，经 antony.best 代理）：按书名/作者/ISBN 找电子书资源。"""
    return core._mk_search_books(rc.deps.ctx)({"query": query, "page": page})


def _search_videos(rc: RunContext[Deps], query: str) -> str:
    """搜影视资源（光影阁聚合源，经 antony.best 代理）：找电影/剧集的在线片源。"""
    return core._mk_search_videos(rc.deps.ctx)({"query": query})


def _kb_mangpai(rc: RunContext[Deps], query: str, type: str = "all") -> str:
    """盲派命理知识库检索（2067 条结构化条目，源自《盲派绝密》等）：查盲派技法/口诀/案例。涉及盲派八字理论问题时先用它查证再回答。"""
    return core._mk_kb_mangpai(rc.deps.ctx)({"query": query, "type": type})


def _web_search(rc: RunContext[Deps], query: str, max_results: int = 5) -> str:
    """联网搜索（AnySearch 全网搜，经 antony.best 代理）：实时新闻/价格/版本/政策等一切模型知识截止日期之后的事实都必须先搜再答，不要凭记忆编时效信息。"""
    return core._mk_web_search(rc.deps.ctx)({"query": query, "max_results": max_results})


def _web_extract(rc: RunContext[Deps], url: str) -> str:
    """抓取指定 URL 的网页正文（搜索结果需要看详情时用）。"""
    return core._mk_web_extract(rc.deps.ctx)({"url": url})


_INTERNAL_FNS = [_q_history, _q_member, _q_stats, _send_message, _generate_image,
                 _search_books, _search_videos, _kb_mangpai, _web_search, _web_extract]


def _budgeted(fn):
    """预算包装：超出 tool_budget 的调用不执行，回填"额度用完"文本
    （与 builtin 循环里的 budget 检查同一语义，且先于框架 UsageLimits 触发）；
    结果截断 result_max_chars；异常转文本不炸循环。"""
    def wrapped(rc: RunContext[Deps], *args, **kwargs):
        d = rc.deps
        if d.ctx["tool_count"] >= d.budget:
            return "工具额度已用完，请直接给最终文字回复。"
        d.ctx["tool_count"] += 1
        t0 = time.time()
        try:
            result = str(fn(rc, *args, **kwargs))
        except Exception as e:
            result = f"工具执行出错：{e}"
        result = result[:d.result_max] + ("\n…(结果过长已截断)" if len(result) > d.result_max else "")
        d.extra_lines.append(
            f"tool={fn.__name__} -> {len(result)}字 ({time.time() - t0:.1f}s)")
        return result
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    wrapped.__annotations__ = fn.__annotations__
    return wrapped


def _gateway_tool(spec: dict):
    """网关工具（antony_*）：原生 JSON schema 注册，执行转发 core._exec_gateway。
    预算/截断语义与内部工具一致。"""
    name = spec["function"]["name"]
    schema = dict(spec["function"].get("parameters") or {"type": "object", "properties": {}})

    def fn(rc: RunContext[Deps], **kwargs: Any) -> str:
        d = rc.deps
        if d.ctx["tool_count"] >= d.budget:
            return "工具额度已用完，请直接给最终文字回复。"
        d.ctx["tool_count"] += 1
        t0 = time.time()
        try:
            result = str(core._exec_gateway(d.ctx["cfg"], d.ctx, name, kwargs))
        except Exception as e:
            result = f"工具执行出错：{e}"
        result = result[:d.result_max] + ("\n…(结果过长已截断)" if len(result) > d.result_max else "")
        d.extra_lines.append(f"tool={name} -> {len(result)}字 ({time.time() - t0:.1f}s)")
        return result

    return Tool.from_schema(fn, name=name,
                            description=spec["function"].get("description") or name,
                            json_schema=schema, takes_ctx=True)


# ================================================================ Agent 构建（按配置指纹缓存）

def _fingerprint(cfg) -> str:
    lcfg = cfg["llm"]
    return json.dumps({
        "base": lcfg.get("base_url"), "model": lcfg.get("model"),
        "fbs": [(f.get("base_url"), f.get("model")) for f in lcfg.get("fallbacks", [])],
        "temp": lcfg.get("temperature"), "thinking": lcfg.get("thinking"),
        "agent": core._acfg(cfg),
    }, ensure_ascii=False, sort_keys=True)


def _build_agent(cfg):
    fp = _fingerprint(cfg)
    if _AGENT_CACHE and _AGENT_CACHE.get("fp") == fp:
        return _AGENT_CACHE["agent"]
    lcfg = cfg["llm"]
    acfg = core._acfg(cfg)

    models = [OpenAIChatModel(lcfg["model"],
                              provider=OpenAIProvider(base_url=lcfg["base_url"],
                                                      api_key=wxbot._api_key(cfg)))]
    for fb in lcfg.get("fallbacks", []) or []:
        key = fb.get("api_key") or wxbot._load_api_key(fb.get("api_key_env", ""))
        if key:
            models.append(OpenAIChatModel(fb["model"],
                                          provider=OpenAIProvider(base_url=fb["base_url"],
                                                                  api_key=key)))
    model = models[0] if len(models) == 1 else FallbackModel(*models)

    settings = {"temperature": lcfg.get("temperature", 0.9),
                "max_tokens": int(acfg.get("max_tokens", 3600)),
                "timeout": 120}
    if lcfg.get("thinking") is not None:
        settings["extra_body"] = {"thinking": lcfg["thinking"]}

    agent = Agent(
        model,
        deps_type=Deps,
        instructions=lambda rc: rc.deps.ctx["system"],   # 每 run 的 system（人格+记忆）
        tools=[_budgeted(f) for f in _INTERNAL_FNS],
        model_settings=ModelSettings(**settings),
        retries=2,
    )

    @agent.output_validator
    def _not_empty(text: str) -> str:
        """StepFun 思考型偶发烧完 token 返回空文本：抛 ModelRetry 让框架重试。"""
        if not text or not text.strip():
            raise ModelRetry("模型返回了空文本，请直接给出最终文字回复")
        return text

    _AGENT_CACHE["fp"] = fp
    _AGENT_CACHE["agent"] = agent
    return agent


# ================================================================ 入口（与 builtin agent_reply 同签名）

def agent_reply(cfg, conversation, inbound, ctx_lines=None, is_group=True,
                username=None, max_rounds=None, tool_budget=None):
    acfg = core._acfg(cfg)
    max_rounds = int(max_rounds or acfg["max_rounds"])
    tool_budget = int(tool_budget or acfg["tool_budget"])
    ctx = core.new_ctx(cfg, conversation, username, inbound, is_group)
    ctx["system"] = wxbot.system_prompt_for(cfg, conversation, inbound, is_group)

    # 用户消息：与 builtin 同一模板（人设体验一致）
    guide = core._tool_guide(core.build_toolset(cfg, ctx))
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

    agent = _build_agent(cfg)

    # 网关工具按消息相关性动态挂（内部工具已在 Agent 上，FunctionToolset 每 run 增补）
    gw_tools = []
    try:
        gw_tools = [_gateway_tool(s) for s in core._gateway(cfg).llm_tools(inbound)]
    except Exception as e:
        print(f"[agent-py] gateway tools unavailable: {e}")

    deps = Deps(ctx=ctx, budget=tool_budget, result_max=int(acfg["result_max_chars"]))
    t0 = time.time()
    try:
        toolsets = [FunctionToolset(tools=gw_tools)] if gw_tools else None
        # request/tool_calls limit 各 +2 做兜底：真正的预算回填在 _budgeted 包装层
        result = agent.run_sync(user_content, deps=deps, toolsets=toolsets,
                                usage_limits=UsageLimits(
                                    request_limit=max_rounds + 2,
                                    tool_calls_limit=tool_budget + 2))
        final_text = result.output
    except AgentRunError as e:
        # UsageLimitExceeded（轮数兜底触发）/ UnexpectedModelBehavior（重试后仍空）：
        # 不 crash——交给收口层（有 outbound/图照样发，否则 [SKIP]），poll 侧零感知
        print(f"[agent-py] run ended without final text ({type(e).__name__}): {str(e)[:120]}")
        final_text = None
    for line in deps.extra_lines:
        print(f"[agent-py] round {line}")
    print(f"[agent-py] done in {time.time() - t0:.1f}s, "
          f"{ctx['tool_count']} tool calls, engine=pydantic")
    return core.finalize_reply(ctx, final_text)
