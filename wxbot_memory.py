# -*- coding: utf-8 -*-
"""
wxbot_memory.py — 双层记忆系统 + workspace 隔离。

层 1（mem0 语义记忆，默认）：mem0ai 嵌入式 —— 本地 qdrant 向量库 + SQLite 历史，
  LLM 自动提取事实并做 ADD/UPDATE/DELETE 去重，回复前按当前消息语义检索 top-k 注入。
  提取 LLM 默认走主通道（OpenAI 兼容），embedding 走 MiniMax embo-01（原生格式适配器）。
层 2（markdown 记忆，兜底+镜像）：MEMORY.md 长期记忆（人工/周期整理）+ 每日笔记。
  mem0 未安装/初始化失败/调用失败时自动退回本层，绝不阻塞回复；
  mem0 提取出的新事实也会镜像到当日笔记，保持 workspace 人类可读。

目录结构（每个对话对象/群聊一个独立 workspace，互相隔离）：

    workspaces/
      <对话slug>-<hash8>/
        MEMORY.md            # 长期记忆（人工/周期整理，注入 system prompt）
        memory/
          YYYY-MM-DD.md      # 每日笔记（自动提取追加，注入最近的）
        files/               # 该对话收到的文件副本（预留）
        notes/               # 其他杂项（预留）
    memory_store/            # mem0 数据（qdrant 向量库 + history.db），按 user_id=对话名 隔离

- slug：对话名清洗（去非法字符）+ sha1 前 8 位防重名/防撞
- 注入：MEMORY.md 前 long_term_chars 字 + 今天/昨天日记各前 daily_chars 字
  + mem0 按当前消息检索的 top-k 条（search_top_k / search_max_chars）
- 提取：每 N 轮回复做一次（every_n_replies），mem0.add 优先，markdown 方案兜底
"""
import os, re, json, time, hashlib, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
WS_ROOT = os.path.join(BASE, "workspaces")

DEFAULTS = {
    "enabled": True,
    "every_n_replies": 5,       # 每 N 轮成功回复做一次记忆提取
    "long_term_chars": 1200,    # MEMORY.md 注入上限
    "daily_chars": 800,         # 每日笔记注入上限
    "extract_max_msgs": 20,     # 提取时参考的最近消息条数
}


def _slug(name):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name or "").strip()[:40]
    h = hashlib.sha1((name or "").encode("utf-8")).hexdigest()[:8]
    return f"{s}-{h}" if s else h


def workspace_for(name):
    """对话 workspace 目录（不存在则建骨架）。返回路径。"""
    d = os.path.join(WS_ROOT, _slug(name))
    os.makedirs(os.path.join(d, "memory"), exist_ok=True)
    os.makedirs(os.path.join(d, "files"), exist_ok=True)
    os.makedirs(os.path.join(d, "notes"), exist_ok=True)
    mem = os.path.join(d, "MEMORY.md")
    if not os.path.exists(mem):
        with open(mem, "w", encoding="utf-8") as f:
            f.write(f"# {name} 的长期记忆\n\n（还没有内容，随着聊天自动积累）\n")
    return d


def _read(path, max_chars):
    try:
        with open(path, "r", encoding="utf-8") as f:
            t = f.read().strip()
        return t[:max_chars] + ("\n…（截断）" if len(t) > max_chars else "") if t else ""
    except Exception:
        return ""


def memory_inject(cfg, name, query=None):
    """读该对话的长期记忆 + 近两天日记 + mem0 语义检索，返回注入 system prompt 的文本（可能为空）。

    query：对方刚发来的消息文本；有它时用 mem0 按相关度捞 top-k 条记忆。"""
    mcfg = (cfg.get("memory") or {})
    if not mcfg.get("enabled", True):
        return ""
    ws = workspace_for(name)
    parts = []
    lt = _read(os.path.join(ws, "MEMORY.md"), int(mcfg.get("long_term_chars", DEFAULTS["long_term_chars"])))
    if lt and "还没有内容" not in lt:
        parts.append(f"【长期记忆】\n{lt}")
    today = datetime.date.today()
    for delta in (0, 1):
        d = today - datetime.timedelta(days=delta)
        p = os.path.join(ws, "memory", f"{d.isoformat()}.md")
        t = _read(p, int(mcfg.get("daily_chars", DEFAULTS["daily_chars"])))
        if t:
            parts.append(f"【{d.isoformat()} 笔记】\n{t}")
    # mem0 语义记忆：按当前消息相关度检索（失败静默降级，不影响回复）
    if query:
        hits = mem0_search(cfg, name, query)
        if hits:
            parts.append("【相关记忆】（按与这条消息的相关度选出，自然使用，别生硬复述）\n" + hits)
    if not parts:
        return ""
    return "\n\n【关于这个对话/对方的记忆（供参考，别生硬复述）】\n" + "\n\n".join(parts)


def should_extract(state, name, every_n):
    """state 里按对话计回复轮数，到 N 的倍数返回 True。"""
    try:
        counts = state.data.setdefault("memory_extract_count", {})
        n = int(counts.get(name, 0)) + 1
        counts[name] = n
        return every_n > 0 and n % every_n == 0
    except Exception:
        return False


def extract_prompt(name, ctx_lines):
    conv = "\n".join(ctx_lines[-DEFAULTS["extract_max_msgs"]:])
    return (
        f"这是微信对话「{name}」最近的聊天记录（我= bot 方，对方= 别人）：\n{conv}\n\n"
        "请提炼值得长期记住的事实，要求：\n"
        "- 只写新出现的、以后聊天用得上的信息（对方透露的偏好/计划/状态/关系/约定/重要事件）\n"
        "- 每条一行，以「- 」开头，不超过 8 条；没有值得记的就只回复 NONE\n"
        "- 不要复述闲聊，不要评价，不要写对话时间"
    )


def append_note(name, lines, tag="自动提取"):
    """把若干条「- 」事实追加到当日笔记（跨方案共用：旧提取 / mem0 镜像）。返回是否写入。"""
    lines = [l for l in lines if l.strip()]
    if not lines:
        return False
    ws = workspace_for(name)
    d = datetime.date.today().isoformat()
    p = os.path.join(ws, "memory", f"{d}.md")
    header = f"# {name} · {d}\n\n" if not os.path.exists(p) else ""
    with open(p, "a", encoding="utf-8") as f:
        if header:
            f.write(header)
        f.write(f"\n## {datetime.datetime.now().strftime('%H:%M')} {tag}\n" + "\n".join(lines) + "\n")
    return True


def store_extraction(name, text):
    """把提取结果追加到当日笔记。返回是否写入。"""
    text = (text or "").strip()
    if not text or text.upper().startswith("NONE"):
        return False
    lines = [l for l in text.splitlines() if l.strip().startswith("-")]
    if not lines:
        return False
    return append_note(name, lines)


# ================================================================ mem0 语义记忆
# 方案：mem0ai 嵌入式用法（本地 qdrant 文件向量库 + SQLite 历史），按 user_id=对话名 隔离；
# LLM 提取走主通道（OpenAI 兼容），embedding 走 MiniMax embo-01（原生格式，见下方适配器）。
# mem0 未安装 / 初始化失败 / 调用失败 → 一律静默降级到上面的 markdown 方案，绝不影响回复。

import threading

STORE_DIR = os.path.join(BASE, "memory_store")   # mem0 数据根目录（qdrant + history.db）
_MEM = {"inst": None, "fp": None, "failed_at": 0.0, "lock": threading.Lock()}
_FAIL_COOLDOWN_S = 600   # 引擎初始化失败后的重试冷却，避免每条消息都撞一次


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
    """取 API key：环境变量 → ~/.openclaw/openclaw.json 的 env 段（同 wxbot._load_api_key）。"""
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


class MiniMaxEmboEmbedder:
    """mem0 embedder 适配器：MiniMax embo-01（原生接口，非 OpenAI 兼容）。

    MiniMax 是双向量设计：入库/更新文本用 type=db，检索查询用 type=query。
    通过 EmbedderFactory.provider_to_class["minimax"] 注册，构造参数复用
    BaseEmbedderConfig 字段（model / api_key / openai_base_url / embedding_dims）。"""

    def __init__(self, config=None):
        self.config = config
        self.model = (getattr(config, "model", None) or "embo-01")
        self.base_url = (getattr(config, "openai_base_url", None) or "https://api.minimaxi.com/v1").rstrip("/")
        self.api_key = getattr(config, "api_key", None) or ""
        if config is not None and not getattr(config, "embedding_dims", None):
            config.embedding_dims = 1536

    def _embed_many(self, texts, emb_type):
        payload = {"model": self.model, "texts": [t if isinstance(t, str) else str(t) for t in texts], "type": emb_type}
        last = None
        for _ in range(2):   # 网络类错误重试一次
            try:
                data = _post_json(self.base_url + "/embeddings", payload, self.api_key, timeout=30)
                st = (data.get("base_resp") or {}).get("status_code", -1)
                vecs = data.get("vectors")
                if st != 0 or not vecs or len(vecs) != len(texts):
                    raise RuntimeError(f"minimax embeddings status={st}")
                return [list(map(float, v)) for v in vecs]
            except Exception as e:
                last = e
                time.sleep(1.0)
        raise RuntimeError(f"minimax embeddings failed: {last}")

    def embed(self, text, memory_action=None):
        return self._embed_many([text], "query" if memory_action == "search" else "db")[0]

    def embed_batch(self, texts, memory_action="add"):
        return self._embed_many(texts, "query" if memory_action == "search" else "db")


def mem0_enabled(cfg):
    m = cfg.get("memory") or {}
    return bool(m.get("enabled", True)) and (m.get("backend", "mem0") == "mem0")


def _resolve_channels(cfg):
    """解析 mem0 用的两个通道：提取 LLM（默认主 llm 通道）+ embedding（默认 MiniMax embo-01）。"""
    e = (cfg.get("memory") or {}).get("mem0") or {}
    ml = e.get("llm") or {}
    if ml.get("base_url"):
        llm = (ml["base_url"].rstrip("/"), ml.get("model"),
               _load_key(ml.get("api_key_env")) or ml.get("api_key") or "", float(ml.get("temperature", 0.2)))
    else:
        lc = cfg.get("llm") or {}
        llm = ((lc.get("base_url") or "").rstrip("/"), lc.get("model"),
               lc.get("api_key") or _load_key(lc.get("api_key_env", "")) or "", 0.2)
    em = e.get("embedder") or {}
    emb = ((em.get("base_url") or "https://api.minimaxi.com/v1").rstrip("/"),
           em.get("model") or "embo-01",
           _load_key(em.get("api_key_env") or "WXBOT_LLM_KEY") or em.get("api_key") or "",
           int(em.get("dims", 1536)))
    return llm, emb


def _get_engine(cfg):
    """懒加载 mem0 Memory 单例（配置指纹变了会重建）；失败进冷却，返回 None。"""
    if not mem0_enabled(cfg):
        return None
    llm, emb = _resolve_channels(cfg)
    fp = json.dumps([llm, emb], ensure_ascii=False)
    with _MEM["lock"]:
        if _MEM["inst"] is not None and _MEM["fp"] == fp:
            return _MEM["inst"]
        if time.time() - _MEM["failed_at"] < _FAIL_COOLDOWN_S:
            return None
        try:
            os.environ.setdefault("MEM0_TELEMETRY", "False")   # 关闭 mem0 遥测（须在 import mem0 前）
            from mem0 import Memory
            from mem0.configs.base import MemoryConfig
            from mem0.utils.factory import EmbedderFactory
            # 注册自定义 embedder。注意：EmbedderConfig 的校验器有写死的 provider 白名单，
            # 所以先以 "openai" 过校验构造 MemoryConfig，再把 provider 翻成 "minimax"
            #（pydantic 默认不在赋值时重校验），Memory.__init__ 会拿它查工厂表命中我们的类。
            EmbedderFactory.provider_to_class["minimax"] = "wxbot_memory.MiniMaxEmboEmbedder"
            m = cfg.get("memory") or {}
            e = m.get("mem0") or {}
            data_dir = e.get("data_dir") or STORE_DIR
            os.makedirs(data_dir, exist_ok=True)
            mc = MemoryConfig(**{
                "llm": {"provider": "openai", "config": {
                    "model": llm[1], "api_key": llm[2], "openai_base_url": llm[0],
                    "temperature": llm[3],
                }},
                "embedder": {"provider": "openai", "config": {
                    "model": emb[1], "api_key": emb[2], "openai_base_url": emb[0],
                    "embedding_dims": emb[3],
                }},
                "vector_store": {"provider": "qdrant", "config": {
                    "path": os.path.join(data_dir, "qdrant"),
                    "collection_name": e.get("collection", "wxbot"),
                    "embedding_model_dims": emb[3],
                }},
                "history_db_path": os.path.join(data_dir, "history.db"),
                "custom_instructions": e.get("custom_instructions") or (
                    "重点提取：对方的称呼与身份关系、偏好厌恶、计划安排、双方约定、"
                    "重要事件、情绪状态。全部用简体中文陈述。"
                ),
            })
            mc.embedder.provider = "minimax"
            inst = Memory(config=mc)
            _MEM["inst"], _MEM["fp"], _MEM["failed_at"] = inst, fp, 0.0
            print(f"[memory] mem0 engine ready (llm={llm[1]} @ {llm[0]}, embed={emb[1]})")
            return inst
        except Exception as ex:
            _MEM["inst"], _MEM["failed_at"] = None, time.time()
            print("[memory] mem0 engine init error:", ex)
            return None


def mem0_add(cfg, name, ctx_lines):
    """把最近聊天喂给 mem0 提取沉淀（自动 ADD/UPDATE/DELETE 去重）。
    成功返回 True；不可用/失败返回 False（调用方退回 markdown 方案）。
    新增事实会镜像一份到当日笔记，保持 workspace 人类可读。"""
    eng = _get_engine(cfg)
    if eng is None:
        return False
    msgs = []
    for line in ctx_lines[-DEFAULTS["extract_max_msgs"]:]:
        line = (line or "").strip()
        if ": " not in line:
            continue
        who, _, text = line.partition(": ")
        role = "assistant" if who == "我" else "user"
        msgs.append({"role": role, "content": text if who in ("我", "对方") else f"{who}：{text}"})
    if not msgs:
        return False
    res = eng.add(msgs, user_id=name) or {}
    events = res.get("results") or []
    added = [e for e in events if e.get("event") in ("ADD", "UPDATE")]
    facts = [f"- {e.get('memory', '').strip()}" for e in added if e.get("memory")]
    if facts:
        append_note(name, facts, tag="mem0 提取")
    print(f"[memory] {name} mem0 add: {len(events)} events ({len(added)} add/update)")
    return True


def mem0_search(cfg, name, query, top_k=None, max_chars=None):
    """按当前消息语义检索该对话的记忆，返回格式化文本（空=无结果/不可用）。"""
    try:
        eng = _get_engine(cfg)
        if eng is None or not (query or "").strip():
            return ""
        m = cfg.get("memory") or {}
        top_k = int(top_k or m.get("search_top_k", 4))
        max_chars = int(max_chars or m.get("search_max_chars", 500))
        threshold = float(m.get("search_threshold", 0.12))
        res = eng.search(query, top_k=top_k, filters={"user_id": name}, threshold=threshold) or {}
        lines, used = [], 0
        for r in res.get("results") or []:
            t = (r.get("memory") or "").strip()
            if not t or used + len(t) > max_chars:
                continue
            lines.append(f"- {t}")
            used += len(t)
        return "\n".join(lines)
    except Exception as e:
        print("[memory] mem0 search error:", e)
        return ""
