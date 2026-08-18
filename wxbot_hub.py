# -*- coding: utf-8 -*-
"""wxbot_hub - antony.best 站点扩展能力（工具网关之外的三个数据源，2026-08-18 接入）

  search_books  GET  /api/annas/search?q=&page=     搜书（Anna's Archive 代理，公开）
  search_videos GET  /api/cine/search?q=&sources=   搜影视（PrivateGate 密语换 token，
                                                     密语在 config hub.cine_gate_answer）
  kb_mangpai    GET  /data/mangpai-kb/index.json    盲派知识库（公开静态 JSON，5.6MB/
                                                     2067 条；检索算法移植自站方
                                                     mangpai-kb/search.ts：关键词字段
                                                     打分 + 同义扩展 + 余弦语义 +
                                                     类型/质量权重，上下文格式与站方
                                                     buildAgentContext 一致）

数据源契约见 D:/玄学/antony-best/web/functions/api/*（站方源码）。
"""
import json
import math
import os
import re
import time
from typing import Dict, List, Optional

DEFAULTS = {
    "enabled": True,
    "base_url": "https://antony.best",
    "cine_gate_answer": "",        # PrivateGate 密语（站主本人填；空=影视搜索禁用）
    "cine_sources": [              # 与站方 DEFAULT_SOURCES 一致
        {"key": "ffzy", "name": "非凡资源", "api": "https://api.ffzyapi.com/api.php/provide/vod"},
        {"key": "lzy", "name": "蓝资源", "api": "https://cj.lziapi.com/api.php/provide/vod"},
        {"key": "bfzy", "name": "暴风资源", "api": "https://bfzyapi.com/api.php/provide/vod"},
    ],
    "mangpai_ttl_s": 7 * 86400,
    "timeout_s": 20,
}

_BASE = os.path.dirname(os.path.abspath(__file__))
_MANGPAI = {"payload": None, "ts": 0.0, "vectors": {}}    # 进程内缓存
_GATE = {"token": None, "exp": 0.0}

# ---------- 站方 search.ts 常量（忠实移植） ----------

_TYPE_WEIGHT = {"method": 1.12, "formula": 1.08, "table": 1.06, "concept": 1.0,
                "case": 0.98, "chapter": 0.72}
_QUALITY_WEIGHT = {"reviewed": 1.16, "merged": 1.12, "machine_validated": 1.08,
                   "draft": 0.94, "needs_review": 0.86, "needs_source_check": 0.78,
                   "needs_ocr_fix": 0.72}
_SYNONYMS = {
    "财": ["钱", "财富", "财运", "妻财", "偏财", "正财"],
    "官": ["官星", "事业", "职位", "仕途", "官贵"],
    "婚姻": ["夫妻", "妻", "夫", "配偶", "感情", "桃花"],
    "子女": ["儿女", "子息", "儿子", "女儿"],
    "父母": ["父亲", "母亲", "父母宫", "年柱"],
    "兄弟": ["姐妹", "兄妹", "同胞"],
    "疾病": ["病", "伤灾", "灾病", "身体", "残疾"],
    "寿命": ["寿", "生死", "夭折", "长寿"],
    "盲派": ["命理", "八字", "四柱"],
    "流年": ["岁运", "大运", "运限", "应期"],
    "刑冲": ["刑", "冲", "害", "合", "破", "六合", "六冲"],
    "墓库": ["库", "墓", "辰戌丑未"],
    "日柱": ["日干", "日主", "日元"],
}
_STOP_WORDS = {"什么", "怎么", "如何", "哪些", "一下", "一个", "这个", "那个",
               "相关", "解释", "查询", "盲派", "八字"}

_PUNCT_RE = re.compile(
    r"[，。！？；：、“”‘’（）()[\]{}《》〈〉【】|/\\.,!?;:\"'`~@#$%^&*_+=-]")


def _hcfg(cfg):
    out = dict(DEFAULTS)
    out.update(cfg.get("hub") or {})
    return out


def _get(url, params=None, timeout=None, headers=None):
    from curl_cffi import requests as creq
    r = creq.get(url, params=params, impersonate="chrome",
                 timeout=timeout or DEFAULTS["timeout_s"],
                 headers=headers or {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    return r


# ================================================================ 搜书（annas）

def search_books(cfg, query: str, page: int = 1) -> str:
    h = _hcfg(cfg)
    if not h["enabled"]:
        return "搜书功能未启用。"
    query = (query or "").strip()[:200]
    if not query:
        return "缺少搜索关键词。"
    try:
        r = _get(f"{h['base_url']}/api/annas/search",
                 {"q": query, "page": max(1, min(50, page))})
        if r.status_code != 200:
            return (f"搜书暂时不可用（上游 {r.status_code}，Anna's Archive 镜像可能失联）。"
                    "请稍后再试，不要编造书目。")
        d = r.json()
        results = (d.get("results") or (d.get("data") or {}).get("results")
                   or d.get("books") or [])
        if not results:
            return f"「{query}」没有搜到书。可以换个书名/作者/ISBN 关键词。"
        lines = [f"「{query}」搜到 {len(results)} 本（第 {page} 页）："]
        for b in results[:8]:
            authors = "、".join((b.get("authors") or [])[:3]) or "佚名"
            meta = " ".join(str(x) for x in (b.get("year"), b.get("format"),
                                             b.get("size"), b.get("language")) if x)
            desc = (b.get("description") or "").strip().replace("\n", " ")[:60]
            lines.append(f"- 《{b.get('title','?')}》{authors}"
                         + (f" ｜{meta}" if meta else "")
                         + (f" ｜{desc}" if desc else ""))
        lines.append("以上来自 Anna's Archive（通过 antony.best 代理）。")
        return "\n".join(lines)
    except Exception as e:
        return f"搜书出错：{str(e)[:120]}。请自然告知用户稍后再试。"


# ================================================================ 搜影视（cine，PrivateGate）

def _gate_token(h) -> Optional[str]:
    answer = (h.get("cine_gate_answer") or "").strip()
    if not answer:
        return None
    if _GATE["token"] and time.time() < _GATE["exp"]:
        return _GATE["token"]
    try:
        from curl_cffi import requests as creq
        r = creq.post(f"{h['base_url']}/api/private-gate/verify",
                      json={"answer": answer}, impersonate="chrome",
                      timeout=h.get("timeout_s", 20))
        d = r.json()
        token = (d.get("data") or {}).get("token") or d.get("token")
        if r.status_code == 200 and token:
            _GATE["token"] = token
            _GATE["exp"] = time.time() + 7 * 3600   # 服务端 TTL 8h，提前 1h 刷新
            return token
        return "WRONG"
    except Exception:
        return "WRONG"


def search_videos(cfg, query: str) -> str:
    h = _hcfg(cfg)
    if not h["enabled"]:
        return "影视搜索未启用。"
    query = (query or "").strip()[:100]
    if not query:
        return "缺少搜索关键词。"
    token = _gate_token(h)
    if token is None:
        return ("影视搜索需要 PrivateGate 密语（配置 hub.cine_gate_answer 为站点的私密答案）。"
                "未配置前请自然告知用户这会儿搜不了影视。")
    if token == "WRONG":
        return "影视搜索密语不对或验证服务暂时不可用，请稍后再试。"
    try:
        r = _get(f"{h['base_url']}/api/cine/search",
                 {"q": query, "sources": json.dumps(h["cine_sources"], ensure_ascii=False)},
                 headers={"Authorization": f"Bearer {token}"})
        if r.status_code != 200:
            return f"影视搜索暂时不可用（{r.status_code}）。请稍后再试。"
        d = r.json()
        results = (d.get("results") or (d.get("data") or {}).get("results")
                   or d.get("videos") or [])
        flat = []
        if results and isinstance(results[0], dict) and "items" in results[0]:
            # 分组形态：[{source_code, source_name, items: [{vod_id, vod_name, vod_remarks...}]}]
            for grp in results:
                for it in grp.get("items") or []:
                    it.setdefault("_src", grp.get("source_name") or grp.get("source_code") or "")
                    flat.append(it)
        elif isinstance(results, dict):
            for src, items in results.items():
                for it in items or []:
                    it.setdefault("_src", src)
                    flat.append(it)
        else:
            flat = results
        if not flat:
            return f"「{query}」没搜到影视资源。可以换个片名试试。"
        lines = [f"「{query}」影视搜索（{d.get('totalItems', len(flat))} 条）："]
        for v in flat[:8]:
            title = v.get("vod_name") or v.get("title") or v.get("name") or "?"
            src = v.get("_src") or v.get("source_name") or v.get("source") or ""
            note = v.get("vod_remarks") or ""
            lines.append(f"- {title}" + (f"（{src}）" if src else "")
                         + (f" ｜{note}" if note else ""))
        lines.append("以上来自光影阁聚合源（通过 antony.best 代理）。")
        return "\n".join(lines)
    except Exception as e:
        return f"影视搜索出错：{str(e)[:120]}。"


# ================================================================ 联网搜索（anysearch，站方代理 AnySearch 服务）

def _anysearch(cfg, command: str, args: dict) -> str:
    """POST /api/anysearch（gate token 鉴权，与 cine 共用密语缓存）。
    站方转发 AnySearch 的 MCP 接口，返回 {ok, text, command, configured}。"""
    h = _hcfg(cfg)
    if not h["enabled"]:
        return "联网搜索未启用。"
    token = _gate_token(h)
    if token is None:
        return ("联网搜索需要 PrivateGate 密语（配置 hub.cine_gate_answer）。"
                "未配置前请自然告知用户搜不了。")
    if token == "WRONG":
        return "搜索密语不对或验证服务暂时不可用，请稍后再试。"
    try:
        from curl_cffi import requests as creq
        r = creq.post(f"{h['base_url']}/api/anysearch",
                      json={"command": command, "args": args},
                      headers={"Authorization": f"Bearer {token}"},
                      impersonate="chrome",
                      timeout=max(h.get("timeout_s", 20), 30))
        d = r.json()
        if r.status_code == 200 and d.get("ok"):
            return d.get("text") or "(搜索返回空结果)"
        return (f"搜索暂时不可用（{r.status_code} {str(d.get('error', ''))[:80]}）。"
                "请稍后再试，不要编造结果。")
    except Exception as e:
        return f"搜索出错：{str(e)[:120]}。请稍后再试。"


def web_search(cfg, query: str, max_results: int = 5) -> str:
    """全网搜索（AnySearch，经 antony.best 代理）。返回 LLM 友好的结果文本。"""
    query = (query or "").strip()[:200]
    if not query:
        return "缺少搜索关键词。"
    return _anysearch(cfg, "search",
                      {"query": query, "max_results": max(1, min(10, max_results))})


def web_extract(cfg, url: str) -> str:
    """抓取指定网页正文（AnySearch extract）。搜索结果需要看详情页时用。"""
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return "url 必须是 http(s) 链接。"
    return _anysearch(cfg, "extract", {"url": url[:2000]})


# ================================================================ 盲派知识库（静态 JSON + 站方检索算法移植）

def _normalize(text: str) -> str:
    t = _PUNCT_RE.sub(" ", (text or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _char_ngrams(token: str) -> List[str]:
    compact = re.sub(r"\s+", "", token)
    return [compact[i:i + n] for n in (2, 3)
            for i in range(0, max(0, len(compact) - n + 1))]


def _tokenize_query(query: str) -> List[str]:
    normalized = _normalize(query)
    if not normalized:
        return []
    terms = set()
    for token in normalized.split(" "):
        if not token or token in _STOP_WORDS:
            continue
        if len(token) <= 24:
            terms.add(token)
        terms.update(_char_ngrams(token))
    for key, values in _SYNONYMS.items():
        if key in query or any(v in query for v in values):
            terms.add(key)
            terms.update(values)
    return [t for t in terms if t]


def _vectorize(text: str) -> Dict[str, int]:
    m: Dict[str, int] = {}
    for term in _tokenize_query(text):
        if term in _STOP_WORDS:
            continue
        m[term] = m.get(term, 0) + 1
    return m


def _cosine(a: Dict[str, int], b: Dict[str, int]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _mangpai_payload(h):
    """5.6MB 索引：进程内缓存 + 磁盘缓存（TTL 天级，避免每次重启都拉 5.6MB）。"""
    if _MANGPAI["payload"] and time.time() - _MANGPAI["ts"] < h["mangpai_ttl_s"]:
        return _MANGPAI["payload"]
    cache = os.path.join(_BASE, "_cache", "mangpai_index.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < h["mangpai_ttl_s"]:
        try:
            with open(cache, "r", encoding="utf-8") as f:
                payload = json.load(f)
            _MANGPAI.update(payload=payload, ts=time.time(), vectors={})
            return payload
        except Exception:
            pass
    r = _get(f"{h['base_url']}/data/mangpai-kb/index.json", timeout=60)
    r.raise_for_status()
    payload = r.json()
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, cache)
    _MANGPAI.update(payload=payload, ts=time.time(), vectors={})
    return payload


def _field_score(item, query: str, terms: List[str]):
    q = _normalize(query)
    name = _normalize(item.get("name"))
    category = _normalize(item.get("category"))
    summary = _normalize(item.get("summary"))
    tags = _normalize(" ".join((item.get("tags") or []) + (item.get("entities") or [])
                               + (item.get("aliases") or [])))
    body = _normalize(item.get("searchText"))
    score = 0.0
    if q and q in name:
        score += 26
    if q and q in category:
        score += 12
    if q and q in summary:
        score += 8
    if q and q in body:
        score += 7
    for term in terms:
        if len(term) < 2:
            continue
        if term in name:
            score += 13
        if term in tags:
            score += 8
        if term in category:
            score += 6
        if term in summary:
            score += 4
        if term in body:
            score += 2.4 if len(term) >= 3 else 1.1
    return score


def kb_mangpai(cfg, query: str, item_type: str = "all") -> str:
    """盲派知识库检索：站方 search.ts 同款算法（字段打分+同义扩展+余弦语义+
    类型/质量权重），返回站方 buildAgentContext 同款资料块 + 使用规则。"""
    h = _hcfg(cfg)
    if not h["enabled"]:
        return "知识库未启用。"
    query = (query or "").strip()
    if not query:
        return "缺少检索问题。"
    try:
        payload = _mangpai_payload(h)
    except Exception as e:
        return f"知识库加载失败：{str(e)[:120]}。请告知用户稍后再试。"
    items = [it for it in (payload.get("items") or [])
             if item_type in ("all", "", None) or it.get("type") == item_type]
    terms = _tokenize_query(query)
    qvec = _vectorize(query)

    scored = []
    semantic_needed = True   # 关键词命中足够时跳过重的余弦计算
    for it in items:
        kw = _field_score(it, query, terms)
        if kw > 0:
            scored.append((kw, 0.0, it))
    if len(scored) >= 3:
        semantic_needed = False
    if semantic_needed:
        for it in items:
            vec = _MANGPAI["vectors"].get(it["id"])
            if vec is None:
                vec = _vectorize(it.get("searchText") or "")
                _MANGPAI["vectors"][it["id"]] = vec
            semi = _cosine(qvec, vec) * 100
            if semi > 0:
                scored.append((0.0, semi, it))
        if scored:
            scored = list({it["id"]: (kw, semi, it) for kw, semi, it in scored}.values())

    def _final(triple):
        kw, semi, it = triple
        quality = _QUALITY_WEIGHT.get((it.get("quality") or {}).get("status"), 0.9)
        typew = _TYPE_WEIGHT.get(it.get("type"), 1.0)
        return ((kw + semi * 0.62) * quality * typew, it)

    ranked = sorted((_final(t) for t in scored), key=lambda x: -x[0])
    ranked = [(s, it) for s, it in ranked if s > 0][:6]
    if not ranked:
        return (f"盲派知识库没检索到「{query}」相关资料。"
                "可以换个关键词（如：墓库、做功、宾主、禄神倒用）。")

    def _clip(text, n):
        t = re.sub(r"\r", "", (text or ""))
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        return t[:n] + "..." if len(t) > n else t

    blocks = []
    for idx, (_s, it) in enumerate(ranked, 1):
        src = it.get("source") or {}
        page = src.get("pageLabel") or (f"page_idx {src['pageIdx']}" if src.get("pageIdx") is not None else "未知页")
        section = src.get("section") or src.get("chapter") or it.get("category") or "未分节"
        q = it.get("quality") or {}
        body = []
        if it.get("summary"):
            body.append(f"摘要：{it['summary']}")
        if it.get("normalizedText"):
            body.append(f"整理：{_clip(it['normalizedText'], 600)}")
        if it.get("contentText"):
            body.append(f"结构：{_clip(it['contentText'], 700)}")
        if it.get("originalText"):
            body.append(f"原文：{_clip(it['originalText'], 500)}")
        blocks.append(
            f"【资料 {idx}】{it.get('name')}\n"
            f"类型：{it.get('typeLabel')}\n分类：{it.get('category') or '未分类'}\n"
            f"来源：{src.get('book', '?')} · {page} · {section}\n"
            f"质量：{q.get('status', 'unknown')}；"
            f"抽取置信度={q.get('extractionConfidence', 'n/a')}；OCR={q.get('ocrConfidence', 'n/a')}\n"
            + "\n".join(body))
    total = payload.get("stats", {}).get("total") or len(payload.get("items") or [])
    return (
        f"盲派知识库（{total} 条，检索「{query}」命中 {len(ranked)} 条）：\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\n使用规则：只依据以上资料回答，资料不足就明说；引用用【资料 N】编号，"
          "不要编造书页章节；质量标记为 needs_*/draft 的依据要提示仍需复核；"
          "保持研究参考口径，不给现实决策结论。")


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)
    sys.path.insert(0, _BASE)
    import wxbot
    cfg = wxbot.load_config()
    t0 = time.time()
    print("=== kb_mangpai 冷启动检索 ===")
    print(kb_mangpai(cfg, "墓库 做功")[:600])
    print(f"...({time.time() - t0:.1f}s)")
    t0 = time.time()
    print("\n=== 热检索（缓存） ===")
    print(kb_mangpai(cfg, "禄神倒用 看子女")[:300])
    print(f"...({time.time() - t0:.1f}s)")
    print("\n=== search_books ===")
    print(search_books(cfg, "三体")[:300])
    print("\n=== search_videos（未配密语应优雅提示） ===")
    print(search_videos(cfg, "让子弹飞")[:150])
