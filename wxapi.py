# -*- coding: utf-8 -*-
"""wxapi - 微信操作本地 API（固定坐标快路径 + 剪贴板媒体发送 + HTTP 封装）

定位：把 wxmini2 的视觉自动化升级为「可编程的操作层」——
- open_chat 快路径：DB 会话顺序(sort_timestamp) ↔ 侧边栏第 N 行固定坐标点击，
  标题区模板比对验证（每会话缓存一张标题截图），OCR 只做兜底
- send_image / send_file：剪贴板 CF_DIB / CF_HDROP 直接 Ctrl+V，零弹窗零坐标
- HTTP API：127.0.0.1 + token，所有 UI 动作经全局单飞锁串行（物理鼠标只有一套）

用法：
    python wxapi.py --serve                 # 起服务（默认 127.0.0.1:8765）
    python wxapi.py --calibrate             # 只标定 layout（侧边栏行距/发送按钮）
    python wxapi.py --cli send-text 文件传输助手 "hello"   # 命令行直调（不经 HTTP）

复用 wxmini2（稳定层）：窗口停靠/前台管理/渲染自愈/像素粘贴判定/DB 解密读取。
配置：wxapi_config.json（{port, token}，首次自动生成随机 token）。
"""
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import argparse
import json
import os
import re
import secrets
import statistics
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "wxapi_config.json")
LAYOUT_PATH = os.path.join(BASE, "wxapi_layout.json")
TITLE_CACHE_DIR = os.path.join(BASE, "wxapi_titles")
SHOT_DIR = os.path.join(BASE, "_wxapi_shots")

sys.path.insert(0, BASE)
import wxmini2 as wx2   # noqa: E402  (导入即完成 DPI 声明)

_UI_LOCK = threading.Lock()          # 单飞锁：同一时刻只允许一个 UI 动作
_LAYOUT = {"loaded": False, "data": None}
_last_open_info = {"method": None}   # 最近一次 open_chat_fast 的验证方式（观测用）


def _log(msg: str):
    print(f"[wxapi] {msg}", flush=True)


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = {"port": 8765, "bind": "127.0.0.1"}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            _log(f"config load error: {e}")
    else:
        cfg["token"] = secrets.token_hex(16)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        _log(f"generated config with new token -> {CONFIG_PATH}")
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ---------------------------------------------------------------- layout 标定

def calibrate_layout(hwnd=None) -> dict:
    """标定 layout（带防重入守卫，实现见 _calibrate_layout_impl）。"""
    _CALIBRATING[0] = True
    try:
        return _calibrate_layout_impl(hwnd)
    finally:
        _CALIBRATING[0] = False


def _calibrate_layout_impl(hwnd=None) -> dict:
    """OCR 一轮侧边栏测行几何（行0中心 y、行距），OCR 定发送按钮坐标。
    全文字行聚类得行距；头部搜索框/标签行靠「与后一行间距 < 0.6*行距」粗丢弃，
    再点第 0 行实测校验（标题 = DB 会话列表第 1 个），错位自动 +1 行修正
    （2026-08-18 实测踩坑：row0 标偏一行会导致点 N 实际点中 N-1）。"""
    from PIL import Image
    hwnd = hwnd or wx2.find_wechat()
    # 先把窗口收敛到生产停靠尺寸：跨屏 DPI 竞争会把窗口缩到 0.8 倍（798×919），
    # 标定必须跑在稳定的 1000×1150 上，否则几何换算全部作废
    for _ in range(3):
        wx2.ensure_window_in_screen(hwnd)
        wx2.park_wechat(hwnd)
        _, _, _, _, cw, chh = wx2.get_window_rect(hwnd)
        if abs(cw - wx2._PARK_W) < 24 and abs(chh - wx2.PARK_H) < 24:
            break
        time.sleep(0.6)
    wx2.force_foreground(hwnd)
    time.sleep(0.3)
    wx2._ensure_fg(hwnd)
    img = wx2._grab_window(hwnd)
    W, H = img.size
    cl, ct = int(W * wx2.LIST_X1), int(H * wx2.LIST_Y1)
    crop = img.crop((cl, ct, int(W * wx2.LIST_X2), int(H * wx2.LIST_Y2)))
    crop2 = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    raw, _ = wx2._get_ocr()(crop2)
    if not raw:
        # 虚拟屏 Qt 间歇停绘会抓到全白——托盘复活后重抓一次再判死
        _log("calibrate: sidebar blank, reviving via tray and re-grabbing")
        wx2.revive_via_tray(hwnd)
        time.sleep(1.0)
        wx2.force_foreground(hwnd)
        time.sleep(0.5)
        img = _grab_window(hwnd)
        W, H = img.size          # 复活切换可能改变窗口尺寸，裁剪必须用新值
        cl, ct = int(W * wx2.LIST_X1), int(H * wx2.LIST_Y1)
        crop = img.crop((cl, ct, int(W * wx2.LIST_X2), int(H * wx2.LIST_Y2)))
        crop2 = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
        raw, _ = wx2._get_ocr()(crop2)
    if not raw:
        raise RuntimeError("calibrate: sidebar OCR empty (window rendered?)")

    def center(bbox):   # 还原到整窗坐标
        return ((bbox[0][0] + bbox[2][0]) / 2 / 2 + cl,
                (bbox[0][1] + bbox[2][1]) / 2 / 2 + ct)

    items = [(center(b), t) for b, t, _ in raw]
    rows = _cluster_ys([y for (x, y), _t in items], gap=30)
    if len(rows) < 3:
        raise RuntimeError(f"calibrate: too few text rows: {rows}")
    pitch = statistics.median([rows[i + 1] - rows[i] for i in range(len(rows) - 1)])
    if not (40 <= pitch <= 110):
        raise RuntimeError(f"calibrate: bad pitch={pitch:.1f}, rows={rows}")
    while len(rows) >= 3 and rows[1] - rows[0] < pitch * 0.6:
        rows.pop(0)          # 搜索框/标签贴着首行（间距远小于行距）→ 丢弃

    # 实测校验：点 row0 应打开某个会话且标题与 DB 前几名之一匹配；
    # 不匹配则整体 +1 行修正（首行常是搜索框文字导致标偏一行）。
    # 渲染检查用安静轮询 _rendered_quiet（不唤醒/不托盘复活）。
    import pyautogui
    l, t, r, b, w, h = wx2.get_window_rect(hwnd)
    db_top_names = [s.get("name", "") for s in wx2.db_sessions(5)]

    for _ in range(4):
        wx2.force_foreground(hwnd)
        pyautogui.click(l + int(w * 0.13), t + int(rows[0]), duration=0.1)
        if not _rendered_quiet(hwnd, wait_s=3.0):
            # 可能是停绘假死，复活后给最后一次机会
            wx2.revive_via_tray(hwnd)
            time.sleep(1.0)
            wx2.force_foreground(hwnd)
            if not _rendered_quiet(hwnd, wait_s=3.0):
                raise RuntimeError("calibrate: window not rendering (park/revive first)")
        title = _title_text(hwnd)
        if any(wx2._title_matches(title, n[:4] if len(n) >= 4 else n[:2])
               for n in db_top_names if n):
            break
        _log(f"calibrate: row0 title {title!r} not in db top5, shifting +{pitch:.0f}px")
        rows = [y + pitch for y in rows]
    else:
        if db_top_names:
            raise RuntimeError("calibrate: row0 live validation failed")

    # 发送按钮：空输入框时微信不显示它——先粘个字让它显形，定位后清草稿
    import pyperclip
    btn = None
    try:
        l, t, r, b, w, h = wx2.get_window_rect(hwnd)
        pyautogui.click(l + int(w * 0.55), t + int(h * 0.89), duration=0.1)
        time.sleep(0.2)
        pyperclip.copy("标")
        pyautogui.hotkey("ctrl", "v", interval=0.05)
        time.sleep(0.6)
        btn = wx2._find_send_button(hwnd)
        pyautogui.hotkey("ctrl", "a", interval=0.03)
        pyautogui.press("delete")
        time.sleep(0.2)
    except Exception as e:
        _log(f"send button probe failed: {e}")
    btn_pct = None
    if btn:
        l, t, r, b, w, h = wx2.get_window_rect(hwnd)
        btn_pct = [round((btn[0] - l) / w, 4), round((btn[1] - t) / h, 4)]
    else:
        btn_pct = [0.955, 0.925]   # 兜底：wxmini2 SEND_X/SEND_Y 经验值

    layout = {
        "window_size": [W, H],
        "row0_y_pct": round(rows[0] / H, 4),
        "row_pitch_pct": round(pitch / H, 4),
        "row_x_pct": 0.13,
        "row_pitch_px": round(pitch, 1),
        "visible_rows": max(1, int((H * wx2.LIST_Y2 - rows[0]) / pitch)),
        "send_btn_pct": btn_pct,
        "title_bbox_pct": [0.31, 0.015, 0.92, 0.078],
        "calibrated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
    _LAYOUT["loaded"], _LAYOUT["data"] = True, layout
    _log(f"layout calibrated: {len(rows)} rows, pitch={pitch:.1f}px, "
         f"row0_y={rows[0]:.0f}px, visible={layout['visible_rows']}, "
         f"send_btn={btn_pct}")
    return layout


def _cluster_ys(ys, gap=30):
    """把 y 坐标按间距 >gap 聚成簇，返回每簇均值（升序）。"""
    ys = sorted(ys)
    out, cur = [], [ys[0]]
    for y in ys[1:]:
        if y - cur[-1] > gap:
            out.append(cur)
            cur = []
        cur.append(y)
    out.append(cur)
    return [statistics.mean(c) for c in out]


DEFAULT_TITLE_BBOX = [0.31, 0.015, 0.92, 0.078]
_CALIBRATING = [False]


def _layout(hwnd=None) -> dict:
    """取 layout；窗口尺寸变了或没标定过就现场重标（需持锁调用）。
    标定进行中直接回现有值/默认值，防止 _title_crop 递归进 calibrate_layout。"""
    if _CALIBRATING[0]:
        return _LAYOUT["data"] or {"title_bbox_pct": DEFAULT_TITLE_BBOX}
    if _LAYOUT["loaded"] and _LAYOUT["data"]:
        lay = _LAYOUT["data"]
        if not hwnd:
            hwnd = wx2.find_wechat()
        _, _, _, _, w, h = wx2.get_window_rect(hwnd)
        if [w, h] == lay["window_size"]:
            return lay
    return calibrate_layout(hwnd)


# ---------------------------------------------------------------- 会话解析

def resolve_contact(contact: str):
    """contact（显示名/群名/username）-> (display_name, username) 或 None。"""
    contact = (contact or "").strip()
    if not contact:
        return None
    if contact in ("filehelper", "文件传输助手"):
        return "文件传输助手", "filehelper"
    sessions = wx2.db_sessions(80)
    for s in sessions:                       # 精确 username 命中
        if s["username"] == contact:
            return s["name"], contact
    u = wx2._name_to_username(contact)       # 按昵称/群名搜
    if u:
        return contact, u
    for s in sessions:                       # 会话列表里名字模糊命中
        if contact in s["name"] or s["name"] in contact:
            return s["name"], s["username"]
    return None


# ---------------------------------------------------------------- 标题验证

def _title_crop(hwnd):
    wx2._ensure_fg(hwnd)
    img = wx2._grab_window(hwnd)
    W, H = img.size
    bbox = (_LAYOUT["data"] or {}).get("title_bbox_pct") or DEFAULT_TITLE_BBOX
    x1, y1, x2, y2 = bbox
    return img.crop((int(W * x1), int(H * y1), int(W * x2), int(H * y2))).convert("L")


def _tpl_path(username: str) -> str:
    import hashlib
    return os.path.join(TITLE_CACHE_DIR,
                        hashlib.md5(username.encode("utf-8")).hexdigest() + ".png")


def _template_score(a, b) -> float:
    """归一化相关系数 0~1。只比左 72% 区域：右端是成员数/未读角标，会变。"""
    import numpy as np
    if a.size != b.size:
        from PIL import Image
        b = b.resize(a.size)
    A = np.asarray(a, dtype=np.float32)
    B = np.asarray(b, dtype=np.float32)
    w = max(1, int(A.shape[1] * 0.72))
    A, B = A[:, :w] - A[:, :w].mean(), B[:, :w] - B[:, :w].mean()
    denom = float(np.sqrt((A * A).sum()) * np.sqrt((B * B).sum()))
    return float((A * B).sum() / denom) if denom > 1e-6 else 0.0


def _title_text(hwnd) -> str:
    """标题区 OCR（取最长一条文字当标题）。"""
    crop = _title_crop(hwnd)
    raw = wx2._ocr_crop_img(crop, scale=2.0)
    return max((t for (_, t, _) in raw), key=len, default="") if raw else ""


def _verify_open(hwnd, display_name: str, username: str):
    """验证当前打开的就是目标会话。模板比对优先（毫秒级），
    无缓存/分数模糊时 OCR 兜底；OCR 确认后回填模板缓存。
    标题渲染晚于聊天内容时 OCR 会抓到聊天区时间戳（如 '昨天 20:09'
    误识为 '距天20:09'）——空结果/时间戳样式一律重试，不直接判负。"""
    import re
    crop = _title_crop(hwnd)
    tpl = None
    p = _tpl_path(username)
    if os.path.exists(p):
        from PIL import Image
        tpl = Image.open(p).convert("L")
    if tpl is not None:
        score = _template_score(tpl, crop)
        if score >= 0.90:
            return True, "template"
    fp = display_name[:4] if len(display_name) >= 4 else display_name[:2]
    text = ""
    for _ in range(4):
        text = _title_text(hwnd)
        if wx2._title_matches(text, fp):
            os.makedirs(TITLE_CACHE_DIR, exist_ok=True)
            crop.save(p)
            return True, "ocr"
        # 空 OCR 或抓到时间戳（标题还没渲染出来）→ 等等再试
        if text and not re.search(r"\d{1,2}\s*:\s*\d{2}|昨天|距天|星期", text):
            break
        time.sleep(0.6)
    return False, (f"ocr-mismatch:{text!r}" if tpl is None else "mismatch")


# ---------------------------------------------------------------- open_chat 快路径

def _rendered_quiet(hwnd, wait_s=2.5) -> bool:
    """安静渲染检查：只轮询像素，不唤醒点击、不托盘复活。
    open 循环里点错行会停在"无会话打开"合法空白态，ensure_chat_rendered
    会误判挂死并对健康窗口托盘开关，把微信弹回主屏（2026-08-18 实测事故）。"""
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            if wx2._chat_content_px(hwnd) > 800:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def open_chat_fast(hwnd: int, contact: str, timeout: float = 10.0,
                   wait_idle: bool = True) -> bool:
    """固定坐标快路径：DB 顺序(sort_timestamp) ≈ 侧边栏顺序，第 N 个会话 →
    点第 N 行；标题模板/OCR 验证；±3 行纠偏（置顶会话插在 row0 之上、打开/
    读取会话刷新 sort，DB 序与侧边栏序会漂移数位）；连续 miss 自动托盘复活
    重试一轮；全败回退 OCR 扫描。签名兼容 wxmini2.open_chat_by_click
    （opener 注入用）。用户正用键鼠时点击会被物理输入打架/前台锁拒绝——先等空闲。"""
    import pyautogui
    if wait_idle and wx2.user_idle_seconds() < 3.0:
        wx2.wait_user_idle(before_s=3.0, max_wait_s=30.0)
    resolved = resolve_contact(contact)
    if not resolved:
        _log(f"open: cannot resolve contact {contact!r}")
        return False
    display_name, username = resolved
    _last_open_info["method"] = None
    wx2.ensure_window_in_screen(hwnd)
    wx2.force_foreground(hwnd)
    time.sleep(0.2)

    # 路线 0：当前已打开目标会话
    ok, how = _verify_open(hwnd, display_name, username)
    if ok and _rendered_quiet(hwnd):
        wx2._remember_opened(display_name, username)
        _last_open_info["method"] = f"already/{how}"
        return True

    try:
        lay = _layout(hwnd)
    except Exception as e:
        _log(f"open: layout calibration failed: {e}")
        return False
    l, t, r, b, w, h = wx2.get_window_rect(hwnd)
    sessions = wx2.db_sessions(80)
    idx = next((i for i, s in enumerate(sessions) if s["username"] == username), None)
    if idx is not None and idx >= lay["visible_rows"]:
        _log(f"open: {display_name!r} is row {idx}, beyond visible "
             f"{lay['visible_rows']} (need scroll) -> OCR fallback")

    if idx is not None and idx < lay["visible_rows"]:
        # 点击/渲染间歇迟滞（虚拟屏 Qt 停绘）时连续 miss——
        # 第一轮全 miss 大概率是窗口僵住，托盘复活后重试一轮（2026-08-18 实测）
        for attempt in range(2):
            misses = 0
            for dy in (0, 1, -1, 2, -2, 3, -3):  # sort 漂移/置顶错位：邻行纠偏
                ry = idx + dy
                if ry >= lay["visible_rows"]:
                    continue
                x = l + int(w * lay["row_x_pct"])
                y = t + int(h * lay["row0_y_pct"] + ry * lay["row_pitch_pct"] * h)
                # 置顶会话会插到 row0 之上（如 filehelper 置顶后占侧边栏第 0 行、
                # DB 按 sort_timestamp 排第 1）——允许负行上探，但不碰搜索框
                if y < t + 100:
                    continue
                wx2.force_foreground(hwnd)
                if ctypes.windll.user32.GetForegroundWindow() != hwnd:
                    _log(f"open: cannot take foreground (user active?), skip row {ry}")
                    continue
                pyautogui.click(x, y, duration=0.1)
                time.sleep(0.6)
                if not _rendered_quiet(hwnd):
                    misses += 1
                    _log(f"open: row {ry} no chat opened (miss?), trying next")
                    continue
                ok, how = _verify_open(hwnd, display_name, username)
                if ok:
                    wx2._remember_opened(display_name, username)
                    _last_open_info["method"] = f"row{ry}{'+' if dy > 0 else ''}/{how}"
                    return True
                _log(f"open: row {ry} verify failed ({how}), trying next")
            if attempt == 0 and misses >= 3:
                _log(f"open: {misses} consecutive misses, reviving via tray and retrying")
                wx2.revive_via_tray(hwnd)
                time.sleep(1.0)
                continue
            break

    # 兜底：先 Esc 关掉可能的搜索浮层（错位点击可能点进搜索框），
    # 再走 wxmini2 的 OCR 扫描路径
    try:
        import pyautogui
        wx2.force_foreground(hwnd)
        pyautogui.press("escape")
        time.sleep(0.4)
    except Exception:
        pass
    if wx2.open_chat_by_click(hwnd, display_name, timeout=timeout):
        _last_open_info["method"] = "ocr_fallback"
        return True
    _log(f"open: all paths failed for {display_name!r}")
    return False


# ---------------------------------------------------------------- 剪贴板

def _clipboard_set(data_type, payload: bytes) -> bool:
    import win32clipboard
    for _ in range(3):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(data_type, payload)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception as e:
            _log(f"clipboard retry: {e}")
            time.sleep(0.3)
    return False


def set_clipboard_image(path: str) -> bool:
    """图片 -> 剪贴板 CF_DIB（24bpp bottom-up BGR，行对齐 4 字节）。"""
    from PIL import Image
    import numpy as np
    img = Image.open(path)
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    arr = np.asarray(img)
    arr = arr[::-1, ::-1]                       # 垂直翻转 + RGB->BGR
    pad = (4 - (w * 3) % 4) % 4
    if pad:
        arr = np.hstack([arr, np.zeros((h, pad, 3), dtype=arr.dtype)])
    head = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0,
                       w * 3 + pad, 2835, 2835, 0, 0)
    return _clipboard_set(8, head + arr.tobytes())   # CF_DIB = 8


def set_clipboard_files(paths) -> bool:
    """文件列表 -> 剪贴板 CF_HDROP（DROPFILES + 双 NUL 结尾宽字符串）。"""
    absp = [os.path.abspath(p) for p in paths]
    for p in absp:
        if not os.path.exists(p):
            _log(f"file not found: {p}")
            return False
    files = "".join(p + "\0" for p in absp) + "\0"
    buf = struct.pack("<IIIii", 20, 0, 0, 0, 1) + files.encode("utf-16-le")
    return _clipboard_set(15, buf)              # CF_HDROP = 15


# ---------------------------------------------------------------- DB 验证

def _db_wait_own_msg(username: str, kind: str, after_ts: float,
                     timeout: float = 20.0, text_contains: str = None):
    """轮询等一条自己发的指定类型消息落库（DB 写入有数秒延迟）。"""
    db = wx2._get_db()
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            msgs = db.get_messages(username, limit=8)
        except Exception:
            msgs = []
        for m in msgs:
            k = wx2._KIND_MAP.get(m.get("type"), m.get("type"))
            if k != kind:
                continue
            if int(m.get("create_time", 0)) < after_ts - 3:
                continue
            if text_contains and text_contains not in (m.get("content") or ""):
                continue
            sid = m.get("sender_id")
            su = wx2._sender_username(db, sid)
            own = (su == db.wxid) if su is not None else (sid == wx2._self_sid(db))
            if own:
                return m
        time.sleep(1.2)
    return None


# ---------------------------------------------------------------- 发送媒体

def _send_media(contact: str, kind: str, clip_setup, after_ts: float,
                wait_idle: bool = True) -> dict:
    """打开会话 -> 点输入框清草稿 -> 设置剪贴板 -> Ctrl+V -> Enter ->
    DB 确认（失败兜底：点发送按钮再等一轮）。kind: 'image' | 'file'。"""
    import pyautogui
    if wait_idle and not wx2.wait_user_idle(max_wait_s=45):
        _log("user still active, sending anyway (timeout)")
    hwnd = wx2.find_wechat()
    wx2.park_wechat(hwnd)
    prev_fg = ctypes.windll.user32.GetForegroundWindow()
    try:
        wx2.force_foreground(hwnd)
        if not open_chat_fast(hwnd, contact, timeout=8.0):
            return {"ok": False, "error": "open_chat failed"}
        if not _rendered_quiet(hwnd, wait_s=4.0):
            return {"ok": False, "error": "chat not rendered"}
        username = (wx2._LAST_OPENED[0] or (None, None))[1]
        if not username:
            return {"ok": False, "error": "target username unknown"}
        if not clip_setup():
            return {"ok": False, "error": "clipboard setup failed"}
        lay = _layout(hwnd)
        l, t, r, b, w, h = wx2.get_window_rect(hwnd)
        wx2.force_foreground(hwnd)
        in_x = l + int(w * 0.55)
        in_y = t + int(h * 0.89)
        pyautogui.click(in_x, in_y, duration=0.1)
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "a", interval=0.03)
        pyautogui.press("delete")
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "v", interval=0.05)
        time.sleep(1.0)
        pyautogui.press("enter")
        time.sleep(1.5)
        m = _db_wait_own_msg(username, kind, after_ts, timeout=8)
        if m is None and lay.get("send_btn_pct"):
            bx, by = lay["send_btn_pct"]
            wx2.force_foreground(hwnd)
            pyautogui.click(l + int(w * bx), t + int(h * by), duration=0.1)
            m = _db_wait_own_msg(username, kind, after_ts, timeout=15)
        if m is None:
            return {"ok": False,
                    "error": f"{kind} not confirmed in DB (draft may remain)"}
        wx2._last_send_ts[0] = time.time()
        return {"ok": True, "kind": kind, "db_confirmed": True,
                "ts": m.get("create_time")}
    finally:
        if prev_fg and prev_fg != hwnd:
            try:
                ctypes.windll.user32.SetForegroundWindow(prev_fg)
            except Exception:
                pass


# ---------------------------------------------------------------- API 动作（持锁入口）

def _acquire(timeout: float = 90.0) -> bool:
    """拿单飞锁。默认排队等 90s（warmup 标定/前一个动作完成）而不是立刻 409。"""
    got = _UI_LOCK.acquire(timeout=timeout)
    if not got:
        raise BusyError()
    return True


class BusyError(Exception):
    pass


def api_health() -> dict:
    sec = wx2.secondary_screen_rect()
    try:
        hwnd = wx2.find_wechat()
        l, t, r, b, w, h = wx2.get_window_rect(hwnd)
        on_virtual = bool(sec) and l >= sec[0] - 8 and r <= sec[0] + sec[2] + 8
        win = {"found": True, "rect": [l, t, w, h], "on_virtual_screen": on_virtual}
        # 登录态探测：侧边栏有内容 = 已登录；空白 = 扫码/确认登录页
        win["logged_in"] = wx2.sidebar_alive_px(hwnd) > wx2.SIDEBAR_ALIVE_TH
    except Exception as e:
        win = {"found": False, "error": str(e)}
    return {"ok": True, "weixin": win,
            "virtual_screen": list(sec) if sec else None,
            "layout": _LAYOUT["data"], "ts": time.time()}


def api_sessions(limit: int = 30) -> dict:
    with _UI_LOCK:
        return {"ok": True, "sessions": wx2.db_sessions(limit)}


def api_chat(username: str, limit: int = 20) -> dict:
    with _UI_LOCK:
        return {"ok": True, "username": username,
                "messages": wx2.read_chat_db(username, limit=limit)}


def api_open(contact: str) -> dict:
    _acquire()
    try:
        hwnd = wx2.find_wechat()
        wx2.park_wechat(hwnd)
        if open_chat_fast(hwnd, contact):
            return {"ok": True, "method": _last_open_info["method"],
                    "opened": list(wx2._LAST_OPENED[0] or (None, None))}
        return {"ok": False, "error": "open failed (see server log)"}
    finally:
        _UI_LOCK.release()


def api_send_text(contact: str, text: str, wait_idle: bool = True) -> dict:
    _acquire()
    try:
        if wait_idle:
            wx2.wait_user_idle(max_wait_s=45)   # 锁内最多等 45s，别把 API 卡死 2 分钟
        opener = lambda h, c, timeout=10.0: open_chat_fast(h, c, timeout)
        ok = wx2.send_text(contact, text, wait_idle=False, open_fn=opener)
        return {"ok": ok, "method": _last_open_info["method"]}
    finally:
        _UI_LOCK.release()


def api_send_image(contact: str, path: str, wait_idle: bool = True) -> dict:
    _acquire()
    try:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return {"ok": False, "error": f"image not found: {path}"}
        return _send_media(contact, "image", lambda: set_clipboard_image(path),
                           time.time(), wait_idle)
    finally:
        _UI_LOCK.release()


def api_send_file(contact: str, paths, wait_idle: bool = True) -> dict:
    _acquire()
    try:
        if isinstance(paths, str):
            paths = [paths]
        return _send_media(contact, "file", lambda: set_clipboard_files(paths),
                           time.time(), wait_idle)
    finally:
        _UI_LOCK.release()


def api_screenshot() -> dict:
    with _UI_LOCK:
        os.makedirs(SHOT_DIR, exist_ok=True)
        hwnd = wx2.find_wechat()
        wx2._ensure_fg(hwnd)
        img = wx2._grab_window(hwnd)
        p = os.path.join(SHOT_DIR, time.strftime("%Y%m%d_%H%M%S") + ".png")
        img.save(p)
        return {"ok": True, "path": p, "size": list(img.size)}


def api_calibrate() -> dict:
    _acquire()
    try:
        return {"ok": True, "layout": calibrate_layout()}
    finally:
        _UI_LOCK.release()


def api_click(x_pct: float, y_pct: float) -> dict:
    """通用原语：按窗口百分比坐标点击（固定坐标控制任意功能的入口）。"""
    import pyautogui
    _acquire()
    try:
        hwnd = wx2.find_wechat()
        wx2.force_foreground(hwnd)
        l, t, r, b, w, h = wx2.get_window_rect(hwnd)
        pyautogui.click(l + int(w * x_pct), t + int(h * y_pct), duration=0.1)
        time.sleep(0.3)
        return {"ok": True, "clicked": [x_pct, y_pct]}
    finally:
        _UI_LOCK.release()


def api_hotkey(keys) -> dict:
    import pyautogui
    _acquire()
    try:
        if isinstance(keys, str):
            keys = [keys]
        hwnd = wx2.find_wechat()
        wx2.force_foreground(hwnd)
        pyautogui.hotkey(*keys, interval=0.05)
        return {"ok": True, "keys": keys}
    finally:
        _UI_LOCK.release()


# ---------------------------------------------------------------- HTTP

ROUTES_POST = {
    "/open": lambda b: api_open(b["contact"]),
    "/send_text": lambda b: api_send_text(b["contact"], b["text"],
                                          bool(b.get("wait_idle", True))),
    "/send_image": lambda b: api_send_image(b["contact"], b["path"],
                                            bool(b.get("wait_idle", True))),
    "/send_file": lambda b: api_send_file(b["contact"], b.get("paths") or b["path"],
                                          bool(b.get("wait_idle", True))),
    "/screenshot": lambda b: api_screenshot(),
    "/click": lambda b: api_click(float(b["x"]), float(b["y"])),
    "/hotkey": lambda b: api_hotkey(b["keys"]),
    "/calibrate": lambda b: api_calibrate(),
}


def make_handler(cfg: dict):
    token = cfg["token"]

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            auth = self.headers.get("Authorization", "")
            key = self.headers.get("X-Token", "")
            return token in (auth.removeprefix("Bearer ").strip(), key)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path == "/health":
                    h = api_health()
                    if not self._authed():
                        h = {"ok": True, "authed": False}
                    return self._send_json(200, h)
                if not self._authed():
                    return self._send_json(401, {"ok": False, "error": "bad token"})
                if u.path == "/sessions":
                    return self._send_json(200, api_sessions(int(q.get("limit", ["30"])[0])))
                if u.path == "/chat":
                    return self._send_json(200, api_chat(q["username"][0],
                                                         int(q.get("limit", ["20"])[0])))
                return self._send_json(404, {"ok": False, "error": "no such route"})
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

        def do_POST(self):
            u = urlparse(self.path)
            if not self._authed():
                return self._send_json(401, {"ok": False, "error": "bad token"})
            fn = ROUTES_POST.get(u.path)
            if not fn:
                return self._send_json(404, {"ok": False, "error": "no such route"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                return self._send_json(200, fn(body))
            except BusyError:
                return self._send_json(409, {"ok": False, "error": "UI busy (action in flight)"})
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})

        def log_message(self, fmt, *args):
            _log(f"http {self.address_string()} {fmt % args}")

    return Handler


def warmup():
    """后台预热：DB 初始化（约 6s）+ layout 加载，让首个请求不卡。"""
    def _w():
        try:
            wx2._get_db()
            hwnd = wx2.find_wechat()
            wx2.park_wechat(hwnd)
            with _UI_LOCK:
                try:
                    _layout(hwnd)
                except Exception as e:
                    _log(f"warmup layout: {e}")
            _log("warmup done")
        except Exception as e:
            _log(f"warmup error: {e}")
    threading.Thread(target=_w, daemon=True).start()


def serve(port: int = None, bind: str = None):
    cfg = load_config()
    port = port or cfg["port"]
    bind = bind or cfg.get("bind", "127.0.0.1")
    warmup()
    srv = ThreadingHTTPServer((bind, port), make_handler(cfg))
    _log(f"listening on http://{bind}:{port} (token in {CONFIG_PATH})")
    srv.serve_forever()


# ---------------------------------------------------------------- CLI 直调

def cli(args):
    load_config()   # 顺带确保 wxapi_config.json + token 存在
    if args.calibrate:
        with _UI_LOCK:
            print(json.dumps(calibrate_layout(), ensure_ascii=False, indent=2))
        return
    if args.serve:
        serve(args.port)
        return
    if args.cli:
        cmd = args.cli[0]
        rest = args.cli[1:]
        if cmd == "sessions":
            r = api_sessions()
        elif cmd == "health":
            r = api_health()
        else:
            contact = rest[0]
            payload = rest[1:]
            if cmd == "send-text":
                r = api_send_text(contact, payload[0], wait_idle=args.wait_idle)
            elif cmd == "send-image":
                r = api_send_image(contact, payload[0], wait_idle=args.wait_idle)
            elif cmd == "send-file":
                r = api_send_file(contact, payload, wait_idle=args.wait_idle)
            elif cmd == "open":
                r = api_open(contact)
            else:
                print(f"unknown cli cmd: {cmd}")
                sys.exit(2)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        sys.exit(0 if r.get("ok") else 1)
    print(__doc__)


def main():
    ap = argparse.ArgumentParser(description="wxapi - WeChat operations API")
    ap.add_argument("--serve", action="store_true", help="start HTTP server")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--calibrate", action="store_true", help="calibrate layout only")
    ap.add_argument("--no-idle-wait", dest="wait_idle", action="store_false",
                    help="cli: skip waiting for user idle")
    ap.add_argument("--cli", nargs="+", metavar="ARG",
                    help="direct call: send-text|send-image|send-file|open|sessions|health")
    args = ap.parse_args()
    if not (args.serve or args.calibrate or args.cli):
        ap.print_help()
        return
    cli(args)


if __name__ == "__main__":
    main()
