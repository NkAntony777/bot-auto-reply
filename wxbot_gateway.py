# -*- coding: utf-8 -*-
"""wxbot_gateway - antony.best 工具网关客户端

契约见 docs/AGENT_ROADMAP.md Phase 2（站侧已上线，2026-08-18 实测通过）：
  GET  /api/v1/tools        工具目录 {name, description, parameters, category, cost_hint}
  POST /api/v1/tools/:name  执行 {success, tool, text, data, meta}；text 为
                            LLM 优化的 canonical 中文 Markdown，直接喂模型
  GET  /api/v1/health       免鉴权存活探测
  鉴权 Bearer <hex token>（token 文件格式 "wxbot:<64hex>"，取冒号后部分）
  限流 60 req/min/token；入口必须走 antony.best（*.workers.dev 被墙）

用法（Phase 1 agent 循环接入）：
    from wxbot_gateway import Gateway
    gw = Gateway(cfg)                      # cfg: wxbot config dict
    tools = gw.llm_tools(message_text)     # 按消息相关性挑工具 → OpenAI tools 格式
    result = gw.call("tarot", {...})       # → {"ok": True, "text": "...", ...}
"""
import json, os, re, time, threading

DEFAULTS = {
    "enabled": True,
    "base_url": "https://antony.best/api/v1",
    "token_file": "D:/玄学/antony-best/web/.tool-gateway-token.local",
    "cache_ttl_s": 3600,
    "timeout_s": 15,
    "max_tools_per_reply": 6,
    "health_check_on_init": True,
}

# 工具相关性关键词（目录 description 匹配不到时的兜底路由提示；
# 站方工具均为命理/卜卦/黄历域，关键词按中文习惯对话触发）
_KEYWORD_HINTS = {
    "bazi": "八字 四柱 生辰 命盘 天干地支 出生日 时辰算命 命理",
    "bazi_dayun": "大运 起运 流年 运势走势 换运",
    "bazi_pillars_resolve": "反推 出生时间 什么时辰生的 四柱反推",
    "ziwei": "紫微 紫微斗数 命宫 十二宫 主星",
    "ziwei_horoscope": "运限 大限 小限 流月 流日 流时",
    "ziwei_flying_star": "飞星 四化 自化 三方四正",
    "liuyao": "六爻 摇卦 排卦 卦象 用神 起卦",
    "qimen": "奇门 奇门遁甲 九宫 八门 九星",
    "taiyi": "太乙 太乙九星 九星阵",
    "xiaoliuren": "小六壬 大安 留连 速喜 赤口 小吉 空亡",
    "daliuren": "大六壬 四课 三传 天地盘 神将",
    "tarot": "塔罗 抽牌 牌阵 占卜 tarot 塔罗牌",
    "almanac": "黄历 宜忌 冲煞 老黄历 今日宜 值星 吉时",
}


class Gateway:
    def __init__(self, cfg=None):
        self.cfg = dict(DEFAULTS)
        if cfg:
            self.cfg.update(cfg.get("gateway") or {})
        self._token = None
        self._catalog = None          # [{name, description, parameters, ...}]
        self._catalog_ts = 0.0
        self._lock = threading.Lock()
        self._http = None             # curl_cffi or urllib poster

    # ---------------- 基础设施 ----------------

    @property
    def token(self):
        if self._token is None:
            raw = ""
            try:
                with open(self.cfg["token_file"], "r", encoding="utf-8-sig") as f:
                    raw = f.read().strip()
            except OSError as e:
                raise RuntimeError(f"gateway token file unreadable: {e}")
            # 文件格式 "wxbot:<hex>"——Bearer 只传 hex 部分（服务端 secret 是 label:token 对）
            self._token = raw.split(":", 1)[1] if ":" in raw else raw
        return self._token

    def _post_json(self, url, payload=None, timeout=None):
        """GET(payload=None)/POST 走 curl_cffi（Chrome TLS 指纹），回退 urllib。"""
        timeout = timeout or self.cfg["timeout_s"]
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            from curl_cffi import requests as creq
            if payload is None:
                r = creq.get(url, headers=headers, timeout=timeout)
            else:
                headers["Content-Type"] = "application/json"
                r = creq.post(url, json=payload, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except ImportError:
            pass
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        if payload is not None:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(payload).encode("utf-8")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---------------- 目录 ----------------

    def health(self):
        try:
            d = self._post_json(f"{self.cfg['base_url']}/health")
            return bool(d.get("ok"))
        except Exception:
            return False

    def catalog(self, force=False):
        """工具目录（缓存 cache_ttl_s）。失败抛异常；空目录返回 []。"""
        with self._lock:
            now = time.time()
            if (not force and self._catalog is not None
                    and now - self._catalog_ts < self.cfg["cache_ttl_s"]):
                return self._catalog
            d = self._post_json(f"{self.cfg['base_url']}/tools")
            tools = d.get("tools") if isinstance(d, dict) else d
            tools = tools or []
            self._catalog = tools
            self._catalog_ts = now
            return tools

    # ---------------- 相关性选择 + LLM tools 转换 ----------------

    def llm_tools(self, message_text="", max_n=None):
        """按消息相关性挑工具并转成 OpenAI function-calling tools 格式。
        相关性：关键词提示逐个在消息里找（中文无空格，不能按消息分词），
        命中关键词长度求和；无命中时回退目录前 3 个（让模型自己看描述挑）。"""
        if not self.cfg["enabled"]:
            return []
        try:
            catalog = self.catalog()
        except Exception as e:
            print(f"[gateway] catalog unavailable: {e}")
            return []
        max_n = max_n or self.cfg["max_tools_per_reply"]
        msg = message_text or ""

        def score(t):
            name = t.get("name", "")
            s = len(name) * 2 if name in msg else 0
            for kw in _KEYWORD_HINTS.get(name, "").split():
                if kw in msg:
                    s += len(kw)
            for kw in re.findall(r"[\u4e00-\u9fff]{2,4}", t.get("description") or ""):
                if kw in msg and len(kw) >= 3:
                    s += len(kw)
            return s

        ranked = sorted(catalog, key=score, reverse=True)
        top_score = score(ranked[0]) if ranked else 0
        if top_score > 0:
            picked = [t for t in ranked if score(t) > 0][:max_n]
        else:
            picked = catalog[:3]
        return [{
            "type": "function",
            "function": {
                "name": f"antony_{t['name']}",
                "description": (t.get("description") or t["name"])[:200],
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            },
        } for t in picked]

    # ---------------- 执行 ----------------

    def call(self, tool_name, params=None, timeout=None):
        """执行工具。tool_name 支持 antony_ 前缀（LLM 调用时带前缀防撞名）。
        返回 {ok, text, data, meta, error}——绝不抛异常（agent 循环友好）。"""
        if not self.cfg["enabled"]:
            return {"ok": False, "error": "gateway disabled"}
        name = tool_name.removeprefix("antony_")
        try:
            d = self._post_json(f"{self.cfg['base_url']}/tools/{name}",
                                params or {}, timeout=timeout)
            if d.get("success"):
                return {"ok": True, "text": d.get("text") or "",
                        "data": d.get("data"), "meta": d.get("meta"), "tool": name}
            return {"ok": False, "error": (d.get("error") or "tool failed"),
                    "text": d.get("text") or "", "tool": name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "tool": name}


if __name__ == "__main__":
    gw = Gateway()
    print("health:", gw.health())
    cat = gw.catalog(force=True)
    print(f"catalog: {len(cat)} tools")
    for t in cat:
        print(f"  {t['name']:24s} [{t.get('category','?')}]")
    tools = gw.llm_tools("帮我用塔罗抽三张牌看看明天运势，顺便看看今日黄历宜忌")
    print(f"\n相关性挑选: {[t['function']['name'] for t in tools]}")
    r = gw.call("tarot", {"question": "bot 工具网关连通性测试", "spreadType": "single",
                          "seed": "wxbot-selftest-001"})
    print(f"\ntarot 调用: ok={r['ok']}")
    print((r.get("text") or r.get("error", ""))[:220])
