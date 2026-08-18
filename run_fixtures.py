# -*- coding: utf-8 -*-
"""run_fixtures - agent 回归重放（docs/RESEARCH_AGENT_FRAMEWORK.md 阶段 A.5）

用法：
  python run_fixtures.py --kind route          # 路由回归：秒级、零 LLM 成本
  python run_fixtures.py --engine builtin      # full 用例走 builtin 引擎（真实 LLM+工具）
  python run_fixtures.py --engine pydantic     # full 用例走 pydantic 引擎
  python run_fixtures.py --engine both         # 双引擎对比
结果落 _fixtures/results/<时间戳>.json；route 用例断言快/慢与 expect 一致，全过退出码 0。
full 用例不做文本断言（LLM 非确定性），只记录输出+工具调用，供人工 diff。
"""
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE = os.path.dirname(os.path.abspath(__file__))
FIX_DIR = os.path.join(BASE, "_fixtures")


def load_fixtures():
    with open(os.path.join(FIX_DIR, "fixtures.json"), encoding="utf-8") as f:
        return json.load(f)["cases"]


def group_target():
    import wxmini2 as wx
    for s in wx.db_sessions(limit=30):
        if "阿布菠萝" in s["name"]:
            return s
    return None


def build_ctx_lines(username, n=10):
    import wxmini2 as wx
    lines = []
    for m in wx.read_chat_db(username, limit=n):
        who = "我" if m["side"] == "own" else (m.get("sender") or "对方")
        txt = m["text"][:100] if m["kind"] == "text" else f"[{m['kind']}]"
        lines.append(f"{who}: {txt}")
    return lines


def run_route(fixtures):
    import wxbot
    import wxbot_agent as core
    cfg = wxbot.load_config()
    passed = failed = 0
    for c in [x for x in fixtures if x["kind"] == "route"]:
        reason = core._route_reason(cfg, c["inbound"], c.get("is_group", True))
        got = "slow" if reason else "fast"
        ok = got == c.get("expect", got)
        print(f"  {'PASS' if ok else 'FAIL'}  {c['id']:22s} expect={c.get('expect'):5s} got={got:5s} ({reason or ''})")
        passed += ok
        failed += (not ok)
    return passed, failed


def run_full(cases, engine):
    import wxbot
    import wxbot_agent as core
    cfg = wxbot.load_config()
    cfg["agent"]["engine"] = engine
    if any(c.get("scenario") == "gateway_down" for c in cases):
        pass  # 单个用例内部切换，见下
    target = group_target()
    if not target:
        print("  (no group target, skip full)")
        return []
    out = []
    for c in cases:
        saved_url = None
        try:
            if c.get("scenario") == "gateway_down":
                saved_url = cfg["gateway"]["base_url"]
                cfg["gateway"]["base_url"] = "https://127.0.0.1:9/api/v1"
                core._GW[0] = None
                gw = core._gateway(cfg)
                gw._catalog = None
                gw._catalog_ts = 0
            t0 = time.time()
            reply = core.agent_reply if engine == "builtin" else core._engine_fn(cfg)[0]
            if engine == "builtin":
                r = core.agent_reply(cfg, target["name"], c["inbound"],
                                     ctx_lines=build_ctx_lines(target["username"]),
                                     is_group=c.get("is_group", True),
                                     username=target["username"])
            else:
                import wxbot_agent_py
                r = wxbot_agent_py.agent_reply(cfg, target["name"], c["inbound"],
                                               ctx_lines=build_ctx_lines(target["username"]),
                                               is_group=c.get("is_group", True),
                                               username=target["username"])
            out.append({"id": c["id"], "engine": engine, "ok": True,
                        "seconds": round(time.time() - t0, 1),
                        "reply": r, "expect_tools": c.get("expect_tools", [])})
            print(f"  DONE {c['id']:22s} {time.time() - t0:5.1f}s  {str(r)[:60]!r}")
        except Exception as e:
            out.append({"id": c["id"], "engine": engine, "ok": False, "error": str(e)[:200]})
            print(f"  CRASH {c['id']:22s} {e}")
        finally:
            if saved_url:
                cfg["gateway"]["base_url"] = saved_url
                core._GW[0] = None
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="route", choices=["route", "full"])
    ap.add_argument("--engine", default="both", choices=["builtin", "pydantic", "both"])
    args = ap.parse_args()
    fixtures = load_fixtures()
    results = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "route": [], "full": []}

    if args.kind == "route":
        print(f"=== route fixtures ({len([c for c in fixtures if c['kind']=='route'])} cases) ===")
        p, f = run_route(fixtures)
        results["route"] = {"passed": p, "failed": f}
        print(f"=== route: {p} passed, {f} failed ===")
        sys.exit(1 if f else 0)

    engines = ["builtin", "pydantic"] if args.engine == "both" else [args.engine]
    full_cases = [c for c in fixtures if c["kind"] == "full"]
    print(f"=== full fixtures ({len(full_cases)} cases x {len(engines)} engines) ===")
    for e in engines:
        print(f"--- engine={e} ---")
        results["full"].extend(run_full(full_cases, e))

    out_path = os.path.join(FIX_DIR, "results",
                            time.strftime("%Y%m%d_%H%M%S") + ".json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"results -> {out_path}")


if __name__ == "__main__":
    main()
