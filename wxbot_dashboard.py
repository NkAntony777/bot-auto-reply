# -*- coding: utf-8 -*-
"""wxbot_dashboard: 本地 Web 看台，监测 wxbot 守护进程状态与日志。

纯标准库实现，绑定 127.0.0.1。数据来源（全部是文件，bot 挂了也能看）：
- wxbot.pid        守护进程 pid + 启动时间
- wxbot_status.json 每轮 poll 的心跳（存活/退避/会话数）
- wxbot_state.json 已读指纹/已回复/发送记账统计
- wxbot_run.log    运行日志尾部
- wxbot_config.json 模型等配置（key 类敏感字段不下发）

用法: python wxbot_dashboard.py [--port 8788] [--bind 127.0.0.1]
自带单实例守卫：启动时清掉旧的 dashboard 进程。
"""
import argparse, json, os, re, subprocess, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(BASE, "wxbot.pid")
STATUS_FILE = os.path.join(BASE, "wxbot_status.json")
STATE_FILE = os.path.join(BASE, "wxbot_state.json")
LOG_FILE = os.path.join(BASE, "wxbot_run.log")
CONFIG_FILE = os.path.join(BASE, "wxbot_config.json")

_SENSITIVE = re.compile(r"key|token|secret", re.I)


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _pid_alive(pid):
    """Windows 进程存活探测：OpenProcess 能打开即存活。
    （别用 os.kill(pid, 0)——Windows 上它直接抛 WinError 87，不是存活检查。）"""
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    ctypes.windll.kernel32.CloseHandle(h)
    return True


def _scrub(obj):
    """递归剔除配置里的 key/token/secret 字段，不下发到页面。"""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if not _SENSITIVE.search(str(k))}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def collect_status():
    now = time.time()
    pid_info = _read_json(PID_FILE) or {}
    hb = _read_json(STATUS_FILE) or {}
    state = _read_json(STATE_FILE) or {}
    cfg = _read_json(CONFIG_FILE) or {}

    pid = pid_info.get("pid")
    alive = bool(pid) and _pid_alive(pid)
    hb_ts = hb.get("ts") or 0
    hb_age = (now - hb_ts) if hb_ts else None
    interval = cfg.get("poll_interval_seconds", 5)
    # 一轮含回复的 poll 可能跑几分钟（agent 循环+拟人延迟+逐句发送），阈值不能太紧
    stale_after = max(6 * interval, 120)

    if not alive:
        bot_status = "down"
    elif hb_age is None or hb_age > stale_after:
        bot_status = "stale"  # 进程活着但心跳停了 → 卡死
    else:
        bot_status = "running"

    backoff_until = hb.get("backoff_until") or 0
    reply_ts = state.get("reply_ts") or {}
    last_replies = sorted(reply_ts.items(), key=lambda kv: kv[1], reverse=True)[:8]

    try:
        log_stat = os.stat(LOG_FILE)
        log_info = {"size": log_stat.st_size, "mtime": log_stat.st_mtime}
    except OSError:
        log_info = {"size": 0, "mtime": 0}

    return {
        "now": now,
        "bot": {
            "status": bot_status,
            "pid": pid if alive else pid,
            "pid_alive": alive,
            "started": pid_info.get("started"),
            "uptime_s": (now - pid_info["started"]) if (alive and pid_info.get("started")) else None,
            "heartbeat_age_s": hb_age,
            "stale_after_s": stale_after,
        },
        "poll": {
            "interval_s": interval,
            "sessions": hb.get("sessions"),
            "replied_last": hb.get("replied"),
            "uia_fail_streak": hb.get("uia_fail_streak"),
        },
        "backoff": {
            "streak": hb.get("backoff_streak") or 0,
            "remaining_s": max(0, backoff_until - now) if backoff_until else 0,
        },
        "state": {
            "seen": len(state.get("seen") or {}),
            "replied_to": len(state.get("replied_to") or {}),
            "sent_recent": len(state.get("sent") or []),
            "last_replies": [{"name": k, "ts": v} for k, v in last_replies],
        },
        "config": {
            "enabled": cfg.get("enabled", True),
            "llm": _scrub(cfg.get("llm") or {}),
            "unlimited_groups": (cfg.get("reply") or {}).get("unlimited_groups", []),
        },
        "log": log_info,
    }


def tail_log(n=200):
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, size - 128 * 1024))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>wxbot 看台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; padding:20px; background:#0f1115; color:#d7dce3;
         font:14px/1.5 "Segoe UI", "Microsoft YaHei", sans-serif; }
  h1 { font-size:18px; margin:0 0 14px; font-weight:600; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
           gap:10px; margin-bottom:16px; }
  .card { background:#181c23; border:1px solid #262c36; border-radius:8px; padding:10px 14px; }
  .card .k { color:#7d8590; font-size:12px; }
  .card .v { font-size:18px; margin-top:2px; font-weight:600; }
  .ok { color:#3fb950; } .bad { color:#f85149; } .warn { color:#d29922; }
  #log { background:#0a0c10; border:1px solid #262c36; border-radius:8px;
         padding:10px 12px; height:55vh; overflow-y:auto;
         font:12px/1.6 Consolas, "Courier New", monospace; white-space:pre-wrap; word-break:break-all; }
  #log .err { color:#f85149; } #log .hl { color:#d29922; } #log .reply { color:#3fb950; }
  .bar { display:flex; gap:14px; align-items:center; margin-bottom:8px; color:#7d8590; font-size:12px; }
  .sub { color:#7d8590; font-weight:400; font-size:12px; }
</style>
</head>
<body>
<h1>wxbot 看台 <span class="sub" id="ts"></span></h1>
<div class="cards" id="cards"></div>
<div class="bar">
  <label><input type="checkbox" id="follow" checked> 跟随滚动</label>
  <span id="loginfo"></span>
</div>
<div id="log"></div>
<script>
const esc = s => s.replace(/&/g,"&amp;").replace(/</g,"&lt;");
const fmtAge = s => s==null ? "—" : s<60 ? Math.round(s)+" 秒前" : s<3600 ? Math.round(s/60)+" 分钟前" : (s/3600).toFixed(1)+" 小时前";
const fmtDur = s => s==null ? "—" : s<60 ? Math.round(s)+"s" : s<3600 ? Math.round(s/60)+"min" : (s/3600).toFixed(1)+"h";
const fmtTs  = t => t ? new Date(t*1000).toLocaleString() : "—";

function card(k, v, cls="", sub="") {
  return `<div class="card"><div class="k">${k}</div><div class="v ${cls}">${v}</div>`
       + (sub?`<div class="k">${sub}</div>`:"") + `</div>`;
}

async function refresh() {
  try {
    const st = await (await fetch("/api/status")).json();
    const b = st.bot;
    const statusMap = { running:["运行中","ok"], stale:["心跳停滞","warn"], down:["已停止","bad"] };
    const [txt, cls] = statusMap[b.status] || ["未知","warn"];
    let cards =
      card("状态", txt, cls, b.pid ? "pid "+b.pid : "无 pid 文件") +
      card("运行时长", fmtDur(b.uptime_s), "", "启动 "+fmtTs(b.started)) +
      card("最近心跳", fmtAge(b.heartbeat_age_s), b.heartbeat_age_s>st.bot.stale_after_s?"warn":"", "阈值 "+Math.round(b.stale_after_s)+"s") +
      card("LLM 退避", st.backoff.remaining_s>0 ? Math.round(st.backoff.remaining_s)+"s" : "无",
           st.backoff.remaining_s>0?"warn":"", "连续失败 "+st.backoff.streak+" 次") +
      card("模型", esc(st.config.llm.model||"—"), "", "fallback "+((st.config.llm.fallbacks||[]).length)+" 个") +
      card("本轮会话", st.poll.sessions ?? "—", st.poll.uia_fail_streak?"warn":"", "上轮回复 "+(st.poll.replied_last??0)+" 条") +
      card("已回复会话", st.state.replied_to, "", "已读指纹 "+st.state.seen);
    if (st.state.last_replies.length) {
      cards += card("最近回复", esc(st.state.last_replies[0].name), "",
        fmtAge(st.now - st.state.last_replies[0].ts));
    }
    document.getElementById("cards").innerHTML = cards;
    document.getElementById("ts").textContent = "更新于 " + new Date().toLocaleTimeString();

    const lg = await (await fetch("/api/log?n=300")).json();
    const box = document.getElementById("log");
    box.innerHTML = lg.lines.map(l => {
      let c = "";
      if (/error|failed|exception|traceback/i.test(l)) c = "err";
      else if (/backoff|skip|throttl|cooldown/i.test(l)) c = "hl";
      else if (/reply to|replied|fallback ok/i.test(l)) c = "reply";
      return `<div class="${c}">${esc(l) || " "}</div>`;
    }).join("");
    document.getElementById("loginfo").textContent =
      "日志 " + (st.log.size/1024).toFixed(0) + " KB · 显示尾部 " + lg.lines.length + " 行";
    if (document.getElementById("follow").checked) box.scrollTop = box.scrollHeight;
  } catch (e) {
    document.getElementById("ts").textContent = "刷新失败: " + e;
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/status":
            self._send(200, json.dumps(collect_status(), ensure_ascii=False))
        elif u.path == "/api/log":
            try:
                n = int((parse_qs(u.query).get("n") or ["200"])[0])
            except ValueError:
                n = 200
            n = max(1, min(1000, n))
            self._send(200, json.dumps({"lines": tail_log(n)}, ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass


def ensure_single_instance():
    """杀掉旧的 dashboard 进程（命令行带 wxbot_dashboard.py 的 python，排除自己和祖先链）。"""
    me = os.getpid()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' } "
             "| ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId) $($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return
    parent = {}
    hits = []
    pat = re.compile(r"wxbot_dashboard\.py(\s|$)")
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parent[pid] = ppid
        if len(parts) == 3 and pat.search(parts[2]):
            hits.append(pid)
    anc = set()
    p = me
    while p and p in parent and p not in anc:
        anc.add(p)
        p = parent[p]
    for pid in hits:
        if pid != me and pid not in anc:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()
    ensure_single_instance()
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"wxbot dashboard: http://{args.bind}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
