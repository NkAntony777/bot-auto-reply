# -*- coding: utf-8 -*-
"""wxapi 端到端测试：起真实服务 → HTTP 全链路 → DB 断言 → 关服务。

目标会话固定为 filehelper（文件传输助手 = 发给自己，零打扰）。
覆盖：health / 鉴权拒绝 / sessions / open（快路径两次，第二次应命中模板缓存）/
send_text / send_image / send_file（DB 类型+方向断言）/ screenshot / 虚拟屏守护。
用法：.venv/Scripts/python.exe -X utf8 wxapi_test.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
PORT = None
TOKEN = None
RESULTS = []


def call(method, path, body=None, auth=True, timeout=300):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    if auth:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def step(name, fn):
    t0 = time.time()
    try:
        detail = fn()
        RESULTS.append((name, "PASS", time.time() - t0, detail))
        print(f"  PASS {name} ({time.time()-t0:.1f}s) {detail}", flush=True)
    except Exception as e:
        RESULTS.append((name, "FAIL", time.time() - t0, str(e)))
        print(f"  FAIL {name} ({time.time()-t0:.1f}s): {e}", flush=True)


def assert_ok(r):
    assert r.get("ok") is True, f"api not ok: {r}"
    return r


def make_assets():
    from PIL import Image, ImageDraw
    d = os.path.join(BASE, "_wxapi_test")
    os.makedirs(d, exist_ok=True)
    ts = time.strftime("%H%M%S")
    img = Image.new("RGB", (360, 240), (24, 26, 32))
    dr = ImageDraw.Draw(img)
    for x in range(0, 360, 20):
        dr.line([(x, 0), (x, 240)], fill=(60, 70, 90), width=2)
    dr.rectangle([60, 60, 300, 180], outline=(230, 160, 60), width=4)
    dr.text((90, 110), f"WXAPI-E2E {ts}", fill=(240, 240, 240))
    img_path = os.path.join(d, f"e2e_{ts}.png")
    img.save(img_path)
    txt_path = os.path.join(d, f"e2e_{ts}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"wxapi e2e file {ts}\n")
    return img_path, txt_path, ts


def t_health():
    h = assert_ok(call("GET", "/health"))
    w = h.get("weixin") or {}
    assert w.get("found"), f"wechat window not found: {h}"
    return f"rect={w.get('rect')} virtual={h.get('virtual_screen')}"


def t_auth_reject():
    try:
        call("GET", "/sessions", auth=False, timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"expected 401, got {e.code}"
        return "401 as expected"
    raise AssertionError("unauthenticated request was accepted")


def t_sessions():
    r = assert_ok(call("GET", "/sessions"))
    s = r["sessions"]
    assert s, "no sessions"
    return f"{len(s)} sessions, top={s[0]['name']!r}"


def t_open_cold():
    r = assert_ok(call("POST", "/open", {"contact": "filehelper"}))
    return f"method={r['method']}"


def t_send_text(msg, tsend):
    def run():
        tsend["text"] = time.time()
        r = assert_ok(call("POST", "/send_text",
                           {"contact": "filehelper", "text": msg}))
        return f"method={r['method']}"
    return run


def t_open_warm():
    r = assert_ok(call("POST", "/open", {"contact": "filehelper"}))
    m = r["method"] or ""
    assert "template" in m or "already" in m, f"fast path missed: {r}"
    return f"method={m}"


def t_confirm_text(msg):
    r = assert_ok(call("GET", "/chat?username=filehelper&limit=10"))
    hit = [m for m in r["messages"] if m["side"] == "own" and m["kind"] == "text"
           and m["text"].strip() == msg]
    assert hit, f"text not in DB: {[(m['kind'], m['side'], m['text'][:30]) for m in r['messages']]}"
    return f"confirmed: {hit[-1]['text'][:44]!r}"


def t_send_image(path, tsend):
    def run():
        tsend["image"] = time.time()
        r = assert_ok(call("POST", "/send_image",
                           {"contact": "filehelper", "path": path}))
        return f"kind={r.get('kind')} db_ts={r.get('ts')}"
    return run


def t_confirm_image(tsend):
    r = assert_ok(call("GET", "/chat?username=filehelper&limit=10"))
    hit = [m for m in r["messages"] if m["side"] == "own" and m["kind"] == "image"
           and m["ts"] >= tsend["image"] - 5]
    assert hit, f"image not in DB: {[(m['kind'], m['side']) for m in r['messages']]}"
    return "image message confirmed in DB"


def t_send_file(path, tsend):
    def run():
        tsend["file"] = time.time()
        r = assert_ok(call("POST", "/send_file",
                           {"contact": "filehelper", "paths": [path]}))
        return f"kind={r.get('kind')} db_ts={r.get('ts')}"
    return run


def t_confirm_file(tsend):
    r = assert_ok(call("GET", "/chat?username=filehelper&limit=10"))
    hit = [m for m in r["messages"] if m["side"] == "own" and m["kind"] == "file"
           and m["ts"] >= tsend["file"] - 5]
    assert hit, f"file not in DB: {[(m['kind'], m['side'], m['text'][:30]) for m in r['messages']]}"
    return f"confirmed: {hit[-1]['text'][:44]!r}"


def t_screenshot():
    r = assert_ok(call("POST", "/screenshot", {}))
    assert os.path.exists(r["path"]), f"screenshot missing: {r}"
    return r["path"]


def t_virtual_guard():
    h = assert_ok(call("GET", "/health"))
    w = h["weixin"]
    assert w.get("on_virtual_screen"), f"wechat left virtual screen: {w}"
    return f"rect={w['rect']}"


def main():
    global PORT, TOKEN
    with open(os.path.join(BASE, "wxapi_config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    PORT, TOKEN = cfg.get("port", 8765), cfg["token"]

    img_path, txt_path, ts = make_assets()
    text_msg = f"wxapi-e2e text {ts}"
    tsend = {}

    print(f"== starting wxapi server on 127.0.0.1:{PORT} ==", flush=True)
    log = open(os.path.join(BASE, "_wxapi_server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [os.path.join(BASE, ".venv", "Scripts", "python.exe"), "-X", "utf8",
         "wxapi.py", "--serve"],
        cwd=BASE, stdout=log, stderr=subprocess.STDOUT)

    try:
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                call("GET", "/health", auth=True, timeout=3)
                break
            except Exception:
                time.sleep(1)
        else:
            print("server did not come up; see _wxapi_server.log", flush=True)
            sys.exit(1)

        step("health", t_health)
        step("auth reject", t_auth_reject)
        step("sessions", t_sessions)
        step("open #1 (cold)", t_open_cold)
        step("send_text", t_send_text(text_msg, tsend))
        step("open #2 (warm cache)", t_open_warm)
        step("DB confirm text", lambda: t_confirm_text(text_msg))
        step("send_image", t_send_image(img_path, tsend))
        step("DB confirm image", lambda: t_confirm_image(tsend))
        step("send_file", t_send_file(txt_path, tsend))
        step("DB confirm file", lambda: t_confirm_file(tsend))
        step("screenshot", t_screenshot)
        step("virtual screen guard", t_virtual_guard)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()

    print("\n== summary ==", flush=True)
    n_pass = sum(1 for r in RESULTS if r[1] == "PASS")
    for name, status, dt, detail in RESULTS:
        print(f"  {status:4s} {name:28s} {dt:6.1f}s  {detail}")
    print(f"\n{n_pass}/{len(RESULTS)} passed", flush=True)
    sys.exit(0 if n_pass == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
