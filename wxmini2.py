"""wxmini2 - WeChat 4.x 视觉版 (PrintWindow + OCR + 坐标点击)

设计原则：
- 抛弃 UIA（微信 4.x 已不暴露 UIA 树）
- 用 PrintWindow 截图（支持 GPU 渲染窗口，不依赖前台）
- 用 RapidOCR 识别中文（裁剪 + 2x 放大提升小窗口准确率）
- 用 hash 差分减少 OCR 调用次数
- 所有坐标用相对百分比，适应窗口大小变化
- 适配小窗口（四分屏，约 480x680 逻辑像素）
"""
# DPI 必须在所有 import 之前
import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import os, re, sys, time, ctypes as C
from ctypes import wintypes
from typing import List, Dict, Optional, Tuple

# ============== 窗口查找 ==============

# 微信 4.x 主窗口类名固定
WX_CLASS = "Qt51514QWindowIcon"

def _enum_wechat_windows() -> List[Tuple[int, bool, str]]:
    """枚举所有微信窗口 (hwnd, visible, title)"""
    EnumWindows = C.windll.user32.EnumWindows
    GetClassNameW = C.windll.user32.GetClassNameW
    GetWindowTextW = C.windll.user32.GetWindowTextW
    IsWindowVisible = C.windll.user32.IsWindowVisible
    out = []
    def cb(hwnd, _):
        cls = C.create_unicode_buffer(256)
        GetClassNameW(hwnd, cls, 256)
        if cls.value == WX_CLASS:
            title = C.create_unicode_buffer(256)
            GetWindowTextW(hwnd, title, 256)
            if title.value.startswith("\u5fae\u4fe1"):
                out.append((hwnd, bool(IsWindowVisible(hwnd)), title.value))
        return True
    WNDENUMPROC = C.WINFUNCTYPE(C.c_bool, C.c_void_p, C.c_void_p)
    EnumWindows(WNDENUMPROC(cb), 0)
    return out

def find_wechat() -> int:
    """找到微信主窗口句柄。如果被最小化会自动恢复。"""
    wins = _enum_wechat_windows()
    for hwnd, vis, _ in wins:
        if vis:
            # 如果被最小化（不可见但存在），尝试恢复
            return hwnd
    # 都没可见，尝试恢复隐藏的
    for hwnd, vis, _ in wins:
        try:
            C.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.8)
            C.windll.user32.SetForegroundWindow(hwnd)
            return hwnd
        except Exception:
            continue
    raise RuntimeError("WeChat main window not found (is \u5fae\u4fe1 running?)")

def force_foreground(hwnd: int):
    """激活窗口到前台。"""
    if C.windll.user32.IsIconic(hwnd):
        C.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    C.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)


def _monitor_rects() -> List[Tuple[int, int, int, int]]:
    """所有监视器的真实矩形列表 [(x,y,w,h), ...]（EnumDisplayMonitors）。"""
    class _MIEX(C.Structure):
        _fields_ = [("cbSize", C.c_uint32), ("rcMonitor", C.c_long * 4),
                    ("rcWork", C.c_long * 4), ("dwFlags", C.c_uint32),
                    ("szDevice", C.c_wchar * 32)]
    found: list = []
    MONITORENUMPROC = C.WINFUNCTYPE(C.c_bool, C.c_void_p, C.c_void_p, C.POINTER(C.c_long * 4), C.c_void_p)
    def _cb(hmon, _hdc, _rect, _lp):
        mi = _MIEX()
        mi.cbSize = C.sizeof(mi)
        if C.windll.user32.GetMonitorInfoW(hmon, C.byref(mi)):
            x, y, r, b = mi.rcMonitor
            found.append((x, y, r - x, b - y))
        return True
    try:
        C.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    except Exception:
        pass
    return found


def ensure_window_in_screen(hwnd: int):
    """保证微信窗口停在目标屏上（虚拟显示器方案的核心守卫）：
    - 有副屏（虚拟显示器）→ 必须整窗在副屏内；微信会被自己的位置记忆/
      托盘复活弹回主屏，发现就停回去
    - 无副屏 → 整窗在任一屏内即可，出界才重新停靠
    不能按"虚拟桌面边界盒"判断——骑跨两屏/落在无监视器区域都会截出黑图。"""
    l, t, r, b, w, h = get_window_rect(hwnd)
    if w <= 0 or h <= 0:
        return
    sec = secondary_screen_rect()
    if sec:
        sx, sy, sw, sh = sec
        if l >= sx - 8 and t >= sy - 8 and r <= sx + sw + 8 and b <= sy + sh + 8:
            return  # 整窗在副屏内
        park_wechat(hwnd)  # 跳去主屏/骑跨了 → 停回来
        return
    for mx, my, mw, mh in _monitor_rects():
        if l >= mx - 8 and t >= my - 8 and r <= mx + mw + 8 and b <= my + mh + 8:
            return
    park_wechat(hwnd)

# ============== 截图 (ImageGrab - 截整个屏幕的窗口区域) ==============
# 关键发现：微信 4.x 聊天区是 Chromium WebView 离屏渲染
# - PrintWindow 抓不到（只抓主窗口 HDC，不含 WebView 渲染）
# - 必须用 ImageGrab 截屏幕缓冲区
# - 代价：窗口必须完全在屏幕内可见（不能超出屏幕边界、不能最小化）

_GetWindowRect = C.windll.user32.GetWindowRect
_GetSystemMetrics = C.windll.user32.GetSystemMetrics
_GetWindowDC = C.windll.user32.GetWindowDC
_GetWindowDC.restype = C.c_void_p          # HDC 是 64 位句柄，必须声明，否则溢出
_ReleaseDC = C.windll.user32.ReleaseDC
_ReleaseDC.argtypes = [C.c_void_p, C.c_void_p]
_PrintWindow = C.windll.user32.PrintWindow
_PrintWindow.argtypes = [C.c_void_p, C.c_void_p, C.c_uint]
_SetCursorPos = C.windll.user32.SetCursorPos

def get_window_rect(hwnd: int) -> Tuple[int, int, int, int, int, int]:
    """返回 (left, top, right, bottom, w, h)"""
    r = wintypes.RECT()
    _GetWindowRect(hwnd, C.byref(r))
    return r.left, r.top, r.right, r.bottom, r.right - r.left, r.bottom - r.top

def get_screen_size() -> Tuple[int, int]:
    """返回 (w, h) 主屏尺寸"""
    return _GetSystemMetrics(0), _GetSystemMetrics(1)


def get_virtual_screen() -> Tuple[int, int, int, int]:
    """返回整个虚拟桌面 (x, y, w, h)——包含主屏+副屏/虚拟屏的全范围。
    多显示器时窗口可以停在副屏（虚拟显示器方案），裁剪必须用这个范围。"""
    return (_GetSystemMetrics(76), _GetSystemMetrics(77),
            _GetSystemMetrics(78), _GetSystemMetrics(79))


def secondary_screen_rect() -> Optional[Tuple[int, int, int, int]]:
    """有副屏（含虚拟显示器）时返回其真实矩形 (x, y, w, h)，否则 None。
    必须用 EnumDisplayMonitors 逐监视器取——虚拟桌面边界盒在多屏高度不一致时
    会给出错误的"副屏高度"（如主屏 1600 高 + 虚拟屏 1200 高 → 误报 1600）。"""

    class _MIEX(C.Structure):
        _fields_ = [("cbSize", C.c_uint32), ("rcMonitor", C.c_long * 4),
                    ("rcWork", C.c_long * 4), ("dwFlags", C.c_uint32),
                    ("szDevice", C.c_wchar * 32)]

    found: list = []
    MONITORENUMPROC = C.WINFUNCTYPE(C.c_bool, C.c_void_p, C.c_void_p, C.POINTER(C.c_long * 4), C.c_void_p)
    def _cb(hmon, _hdc, _rect, _lp):
        mi = _MIEX()
        mi.cbSize = C.sizeof(mi)
        if C.windll.user32.GetMonitorInfoW(hmon, C.byref(mi)):
            x, y, r, b = mi.rcMonitor
            primary = bool(mi.dwFlags & 1)
            found.append((primary, x, y, r - x, b - y))
        return True
    try:
        C.windll.user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    except Exception:
        return None
    for primary, x, y, w, h in found:
        if not primary and w > 300 and h > 300:
            return (x, y, w, h)
    return None


def clip_to_screen(l: int, t: int, r: int, b: int) -> Tuple[int, int, int, int]:
    """把窗口矩形裁剪到虚拟桌面范围内（多显示器安全）"""
    vx, vy, vw, vh = get_virtual_screen()
    l = max(vx, min(vx + vw - 1, l))
    t = max(vy, min(vy + vh - 1, t))
    r = max(vx, min(vx + vw, r))
    b = max(vy, min(vy + vh, b))
    return l, t, r, b

def pw_shot(hwnd: int):
    """截图（PrintWindow flag=0 方式）。
    比 ImageGrab 更稳定：不受其他窗口遮挡影响，截到的是目标窗口 HDC 的内容。
    返回 PIL Image。原点在窗口左上角。
    """
    from PIL import Image
    import win32ui
    l, t, r, b, w, h = get_window_rect(hwnd)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"window rect invalid: {l},{t},{r},{b}")
    hdc = _GetWindowDC(hwnd)
    try:
        save_dc = win32ui.CreateDCFromHandle(hdc).CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(win32ui.CreateDCFromHandle(hdc), w, h)
        save_dc.SelectObject(bmp)
        # flag=0 (PW_CLIENTONLY | 普通过程)，实测微信 4.x 能截到完整内容
        ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        return Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']),
                               bits, 'raw', 'BGRX', 0, 1)
    finally:
        try: save_dc.DeleteDC()
        except: pass
        try: win32ui.DeleteObject(bmp.GetHandle())
        except: pass
        _ReleaseDC(hwnd, hdc)

# ============== OCR (RapidOCR 懒加载) ==============

_ocr = None

def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr

# 裁剪区域（窗口内百分比）
# 微信 4.x 窗口布局（基于实测）：
#   顶部标题栏  ~10%   (排除，避免被 OCR 误读)
#   左侧会话列表 0~28% / 10~95%
#   中间聊天区   30~78% / 14~85%  (避开标题栏)
#   顶部聊天名  50~78% / 4~10%   (右侧区域)
#   底部输入框   ~92%
LIST_X1, LIST_X2 = 0.00, 0.28
LIST_Y1, LIST_Y2 = 0.10, 0.95
CHAT_X1, CHAT_X2 = 0.30, 0.78
CHAT_Y1, CHAT_Y2 = 0.14, 0.85   # 避开顶部标题栏
TITLE_X1, TITLE_X2 = 0.50, 0.78
TITLE_Y1, TITLE_Y2 = 0.04, 0.10
INPUT_X = 0.60
INPUT_Y = 0.92
SEND_X = 0.96
SEND_Y = 0.92

def _crop_pct(img, x1, y1, x2, y2):
    w, h = img.size
    return img.crop((int(w*x1), int(h*y1), int(w*x2), int(h*y2)))

def _dark_filter(crop, dark_th=80, min_pixels=10):
    """快速判断 crop 区域是否有深色文字（避免无文字的空白也调 OCR）"""
    import numpy as np
    arr = C.c_uint8 * 0  # placeholder
    arr = np.array(crop)
    dark = (arr[:,:,0] < dark_th) & (arr[:,:,1] < dark_th) & (arr[:,:,2] < dark_th)
    return int(dark.sum()) > min_pixels

def _ocr_crop(img, x1, y1, x2, y2, scale=2.0, min_conf=0.4):
    """裁剪 + 放大 + OCR，返回 [(bbox, text, conf), ...]"""
    crop = _crop_pct(img, x1, y1, x2, y2)
    if not _dark_filter(crop):
        return []
    if scale != 1.0:
        from PIL import Image
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)),
                            Image.LANCZOS)
    ocr = _get_ocr()
    result, _ = ocr(crop)
    if not result:
        return []
    # 过滤低置信度
    return [(b, t, c) for (b, t, c) in result if c >= min_conf]

# ============== 会话列表读取 ==============

def list_sessions(hwnd: int = None, retries: int = 3) -> List[Dict]:
    """返回 [{name, last, raw}] for each conversation in the left sidebar.
    重试 3 次以容忍窗口尚未渲染的情况。"""
    from difflib import SequenceMatcher
    hwnd = hwnd or find_wechat()
    img = None
    for _ in range(max(1, retries)):
        img = pw_shot(hwnd)
        if img is not None:
            break
        time.sleep(0.4)
    if img is None:
        return []
    scale = 2.0
    raw = _ocr_crop(img, LIST_X1, LIST_Y1, LIST_X2, LIST_Y2, scale=scale, min_conf=0.4)
    out = []
    w, h = img.size
    for bbox, text, conf in raw:
        # 计算在原图中的 y/x 中心（scale 还原）
        cy = (bbox[0][1] + bbox[2][1]) / 2 / scale / h
        cx = (bbox[0][0] + bbox[2][0]) / 2 / scale / w
        # 同一行检测：cx 在前一个右侧（时间戳、消息预览），cy 接近
        # 左侧名字在 x < 0.18，右侧预览/时间在 x > 0.18
        existing = None
        for s in out:
            # 同行判定：cy 差 < 0.018（约 8 像素）
            # 且一个在左一个在右（一个 cx<0.18 一个 cx>0.18）
            if abs(s['rect'][1] - cy) < 0.018:
                # 如果当前文字在右侧，且已有的是左侧名字 → 合并为 last
                if cx > 0.18 and s['rect'][0] < 0.18:
                    existing = s
                    break
        if existing:
            existing['last'] = (existing.get('last', '') + ' ' + text).strip()
            existing['raw'] = (existing.get('raw', '') + ' ' + text).strip()
        else:
            out.append({
                'name': text,
                'last': text,
                'raw': text,
                'conf': conf,
                'rect': (cx, cy),
            })
    return out

# ============== 当前聊天读取（数据库版，绕过 WebView 渲染问题） ==============

# 模块级：记录当前打开的会话 (nick, username)
_LAST_OPENED = [None]  # [(nick_name, username)]
_WECHAT_DB = [None]     # WeChatDB 单例（懒加载）


def _patch_friendly_content():
    """给 wechatauto 补上真正的 zstd 解压（猴子补丁，包本体不动）。

    微信 4.x 会把长/多行文本、表情 XML 的 message_content 用 zstd 压缩存储；
    wechatauto 的 _extract_text_from_blob 只是"跳过容器头找明文"的启发式，压实的
    blob 解不出就退化成 '[文本]' 占位符——read_chat_db 读不到真实内容，
    _send_text_core 的 DB 精确匹配发送确认也会误报失败。
    只对文本类消息启用解压；表情/图片保持占位符语义（XML 对上下文无用）。"""
    try:
        import zstandard as _zstd
        from wechatauto.db import WeChatDB as _WDB, _extract_text_from_blob as _heuristic
    except ImportError:
        return

    def _friendly(content: bytes, mtype) -> str:
        try:
            text = content.decode("utf-8")
            return text.strip() or f"[{mtype}]"
        except UnicodeDecodeError:
            pass
        if mtype == "文本" and content[:4] == b"\x28\xb5\x2f\xfd":
            try:
                raw = _zstd.ZstdDecompressor().decompress(content, max_output_size=1 << 20)
                return raw.decode("utf-8", "replace").strip() or f"[{mtype}]"
            except Exception:
                pass
            text = _heuristic(content)
            if text:
                return text
        if mtype == "图片":
            md5 = re.search(rb'md5="([0-9a-fA-F]{32})"', content)
            if md5:
                return f"[图片 md5={md5.group(1).decode()}]"
        return f"[{mtype}]"

    _WDB._friendly_content = staticmethod(_friendly)


def _get_db():
    """懒加载 WeChatDB 单例（首次初始化约 6 秒）"""
    if _WECHAT_DB[0] is None:
        from wechatauto import WeChatDB
        _patch_friendly_content()
        _WECHAT_DB[0] = WeChatDB()
    return _WECHAT_DB[0]


def _name_to_username(name: str) -> Optional[str]:
    """nick_name / 群名 / 部分匹配 -> username（wxid / chatroom id）"""
    if not name:
        return None
    try:
        db = _get_db()
        hits = db.search_contact(name)
        if hits:
            return hits[0]["username"]
    except Exception as e:
        print(f"[wxmini2] search_contact error: {e}")
    return None


# ---- DB 权威读取：会话列表 + 按 username 读消息（轮询主路径） ----

_NICK_CACHE: Dict[str, str] = {}
_SENDER_IDX: List[Optional[Dict[int, str]]] = [None]  # sender_id -> username 懒加载

# db.get_messages 返回的中文类型名 -> wxbot 内部 kind
_KIND_MAP = {
    "文本": "text", "图片": "image", "文件/链接/卡片": "file",
    "语音": "voice", "视频": "video", "动画表情": "sticker",
    "位置": "location", "系统消息": "system",
}


def _nick_of(db, username: str) -> str:
    if username not in _NICK_CACHE:
        try:
            _NICK_CACHE[username] = db.get_nickname(username) or username
        except Exception:
            _NICK_CACHE[username] = username
    return _NICK_CACHE[username]


def _sender_username(db, sender_id) -> Optional[str]:
    """real_sender_id(数字) -> username，查 message_resource.db 的 SenderName2Id。
    映射表缺失时返回 None（由 _self_sid 兜底判边）。
    注意：不能硬编码 sid=2=自己——本机实测 2 是别的群员。"""
    try:
        if _SENDER_IDX[0] is None:
            _SENDER_IDX[0] = db._sender_id_index()
        return _SENDER_IDX[0].get(int(sender_id))
    except Exception:
        return None


_SELF_SID: List[Optional[int]] = [None]

def _self_sid(db) -> int:
    """自己账号的 real_sender_id。filehelper（文件传输助手）里全部是自己发的消息，
    取最新一条的 sender_id 即可权威定位（实测本机=3，不能硬编码 2）。"""
    if _SELF_SID[0] is None:
        try:
            fh = db.get_messages("filehelper", limit=3)
            _SELF_SID[0] = int(fh[0]["sender_id"]) if fh else 3
        except Exception:
            _SELF_SID[0] = 3
    return _SELF_SID[0]


def db_sessions(limit: int = 30) -> List[Dict]:
    """数据库会话列表（权威）：完整群名/昵称 + username + 预览。
    替代 OCR 列表：OCR 会截断长群名、丢灰色预览、产生碎片噪声。"""
    db = _get_db()
    out = []
    for s in db.get_sessions(limit=limit):
        u = s.get("username") or ""
        if not u:
            continue
        summary = s.get("summary") or ""
        if isinstance(summary, bytes):
            summary = summary.decode("utf-8", "replace")
        summary = summary.strip()
        out.append({
            "name": _nick_of(db, u),
            "username": u,
            "last": summary,
            "raw": summary,
            "unread": int(s.get("unread") or 0),
        })
    return out


def read_chat_db(username: str, limit: int = 20) -> List[Dict]:
    """按 username 直接读数据库消息（不依赖窗口/OCR）。
    返回正序 [{kind, text, side, sender, ts}]；
    side 判定：SenderName2Id 映射对照 db.wxid（权威），映射缺失时用
    filehelper 探测的自 sid 兜底。群消息内容剥离 'wxid_xxx:\n' 前缀。
    DB 连接坏损（malformed/unpack，微信活跃写入时偶发）自动重置单例下轮重建。"""
    db = _get_db()
    try:
        msgs = db.get_messages(username, limit=limit)
    except Exception as e:
        msg = str(e)
        print(f"[wxmini2] read_chat_db error: {msg[:80]}")
        if "malformed" in msg or "unpack" in msg or "database is locked" in msg:
            _WECHAT_DB[0] = None   # 重置单例：下次调用重建连接（重新做 WAL 合并）
            print("[wxmini2] db unhealthy, singleton reset for rebuild")
        return []
    wxid = db.wxid
    is_chatroom = username.endswith("@chatroom")
    out = []
    for m in reversed(msgs):  # db 返回倒序（最新在前），还原成正序
        t = m.get("type")
        kind = _KIND_MAP.get(t) or ("text" if t == 1 else f"type{t}")
        sid = m.get("sender_id")
        content = m.get("content", "") or ""
        sender_hint = None
        if is_chatroom:
            # 群消息格式 'wxid_xxx:\n正文'（发送者非自己时带前缀）
            pm = re.match(r"^([A-Za-z0-9_-]{5,40}):\n(.*)$", content, re.S)
            if pm:
                sender_hint = pm.group(1)
                content = pm.group(2)
        su = _sender_username(db, sid)
        if su is not None:
            side = "own" if su == wxid else "other"
        else:
            side = "own" if sid == _self_sid(db) else "other"
        if side == "own":
            sender = "我"
        elif su:
            sender = _nick_of(db, su)
        elif sender_hint:
            sender = _nick_of(db, sender_hint)
        else:
            sender = "对方"
        out.append({
            "kind": kind,
            "text": content,
            "side": side,
            "sender": sender,
            "ts": m.get("create_time", 0),
        })
    return out


def read_chat(hwnd: int = None, limit: int = 5, detect_side: bool = True) -> List[Dict]:
    """读"当前打开会话"的最新消息（从 SQLCipher 数据库）。
    依赖 open_chat_by_click 设置的 _LAST_OPENED；判边/类型走 read_chat_db。"""
    if not _LAST_OPENED[0]:
        return []
    nick, username = _LAST_OPENED[0]
    if not username:
        # 兜底：用 nick 试着搜一次
        username = _name_to_username(nick)
        if username:
            _LAST_OPENED[0] = (nick, username)
        else:
            return []
    return read_chat_db(username, limit=limit)

def _ocr_crop_img(crop, scale=2.0):
    """对 PIL crop 做 OCR，尺寸保护（窗口动画/恢复期可能拿到空图）。"""
    from PIL import Image
    if crop is None or crop.width < 12 or crop.height < 6:
        return []
    if scale != 1.0:
        crop = crop.resize((int(crop.width * scale), int(crop.height * scale)), Image.LANCZOS)
    raw, _ = _get_ocr()(crop)
    return raw or []


def current_chat_name(hwnd: int = None) -> Optional[str]:
    """读取当前打开聊天的标题（顶部区域，ImageGrab + 无深色过滤）。
    浅色主题文字是深色没问题；深色主题文字偏亮，故不用 dark_filter。"""
    hwnd = hwnd or find_wechat()
    img = _grab_window(hwnd)
    W, H = img.size
    raw = _ocr_crop_img(img.crop((int(W*0.33), int(H*0.015), int(W*0.92), int(H*0.09))))
    if raw:
        return max((t for (_, t, _) in raw), key=len, default=None)
    return None

# ============== 点击打开会话 ==============

def _find_sidebar_row(hwnd: int, fingerprint: str):
    """OCR 左侧会话列表（ImageGrab 屏幕截取，PrintWindow 偶发空白帧不可靠），
    找含 fingerprint 的行，返回 (窗口内像素 x, y) 或 None。
    常规裁剪 (LIST_*) 找不到时，用从窗口顶开始的扩展裁剪再扫一遍——
    有新消息的会话会顶到列表第一行，固定 LIST_Y1=0.10 的上边距在小窗口
    （停靠虚拟屏后约 1000px 高）会把首行群名切掉。"""
    from PIL import Image
    img = _grab_window(hwnd)
    W, H = img.size
    for y1 in (LIST_Y1, 0.02):
        crop = _crop_pct(img, LIST_X1, y1, LIST_X2, LIST_Y2)
        crop2 = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
        raw, _ = _get_ocr()(crop2)
        if not raw:
            continue
        CH, CW = crop.height, crop.width
        for bbox, text, conf in raw:
            if conf < 0.5 or not text:
                continue
            cy = (bbox[0][1] + bbox[2][1]) / 2 / 2 / CH
            cx = (bbox[0][0] + bbox[2][0]) / 2 / 2 / CW
            if fingerprint and fingerprint in text:
                px = int((LIST_X1 + (LIST_X2 - LIST_X1) * min(max(cx, 0.1), 0.9)) * W)
                py = int((y1 + (LIST_Y2 - y1) * min(max(cy, 0.0), 1.0)) * H)
                return px, py
    return None


def _title_matches(title: Optional[str], fingerprint: str) -> bool:
    """标题模糊匹配：OCR 会丢首字/加成员数后缀，严格子串会误判。"""
    if not title or not fingerprint:
        return False
    t = re.sub(r"\(\d+\)\s*$", "", title.strip())
    if fingerprint in t:
        return True
    if len(fingerprint) >= 3 and fingerprint[1:] in t:   # OCR 丢首字
        return True
    if len(fingerprint) >= 3 and fingerprint[:-1] in t:  # OCR 丢尾字
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, fingerprint, t).ratio() > 0.55


def open_chat_by_click(hwnd: int, name: str, timeout: float = 10.0) -> bool:
    """打开指定会话：当前已是目标(标题匹配) → 直接用；否则点左侧列表条目。
    不再用 Ctrl+F 搜索：搜索面板 pw_shot/ImageGrab 都抓不到，且 escape/搜索
    组合键疑似触发微信 Qt 渲染挂死。记录 _LAST_OPENED 供 read_chat 用。
    """
    import pyautogui

    target_username = _name_to_username(name)
    name_clean = name.replace('...', '').replace('…', '').strip()
    fingerprint = name_clean[:4] if len(name_clean) >= 4 else name_clean[:2]

    ensure_window_in_screen(hwnd)
    force_foreground(hwnd)
    time.sleep(0.2)

    # 路线 0：当前打开的已经就是目标会话（标题含指纹）→ 直接用
    title = current_chat_name(hwnd)
    if _title_matches(title, fingerprint):
        if ensure_chat_rendered(hwnd):
            _remember_opened(name, target_username)
            return True

    # 路线 1：侧边栏直接点击（ImageGrab 扫描；空白时唤醒重试一轮）
    pos = _find_sidebar_row(hwnd, fingerprint)
    if pos is None:
        ensure_chat_rendered(hwnd, timeout=3.0)  # 顺带唤醒渲染
        pos = _find_sidebar_row(hwnd, fingerprint)
    if pos:
        l, t, r, b, _, _ = get_window_rect(hwnd)
        import pyautogui
        pyautogui.click(l + pos[0], t + pos[1], duration=0.12)
        time.sleep(0.6)
        # 切会话后 Qt 可能不渲染（一片空白）——强制唤醒
        ensure_chat_rendered(hwnd)
        # 复核标题（防止点错行）
        title = current_chat_name(hwnd)
        if _title_matches(title, fingerprint):
            _remember_opened(name, target_username)
            return True
        print(f"[wxmini2] sidebar click landed on wrong chat: {title!r}")

    print(f"[wxmini2] open_chat failed: sidebar miss {fingerprint!r}")
    return False


def _remember_opened(name: str, target_username):
    """记录当前打开的会话；username 缺失时按 DB 名字补全。"""
    nick = name
    if target_username:
        try:
            db_nick = _get_db().get_nickname(target_username)
            if db_nick:
                nick = db_nick
        except Exception:
            pass
    _LAST_OPENED[0] = (nick, target_username or None)

# ============== 发送消息 ==============

_MIN_SEND_GAP_S = 1.0
_last_send_ts = [0.0]

# 用户活跃规避：用户正在用键鼠时推迟发送（避免抢输入），超时后强制发
_IDLE_BEFORE_SEND_S = 6.0     # 用户静默多久才算"闲"
_IDLE_MAX_WAIT_S = 120.0      # 最多等多久
# 常驻停靠：缩小并停在屏幕右下角，不占主工作区
# 注意：再小 OCR 误字率会飙升（760px 时一半字读错），1000 是实测下限
_PARK_W, PARK_H = 1000, 1150


class _LASTINPUTINFO(C.Structure):
    _fields_ = [("cbSize", C.c_uint), ("dwTime", C.c_uint)]


def user_idle_seconds() -> float:
    """系统级键鼠空闲秒数（GetLastInputInfo，全部会话共享）。"""
    lii = _LASTINPUTINFO()
    lii.cbSize = C.sizeof(lii)
    if not C.windll.user32.GetLastInputInfo(C.byref(lii)):
        return 999.0
    return max(0.0, (C.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)


def wait_user_idle(before_s: float = _IDLE_BEFORE_SEND_S,
                   max_wait_s: float = _IDLE_MAX_WAIT_S) -> bool:
    """等到用户闲下来；返回是否等到（超时返回 False 但调用方仍可继续）。"""
    t0 = time.time()
    while time.time() - t0 < max_wait_s:
        if user_idle_seconds() >= before_s:
            return True
        time.sleep(0.5)
    return False


def _move_window_to(hwnd: int, sx: int, sy: int, sw: int, sh: int,
                    req_w: int, req_h: int, center: bool):
    """把窗口移动+调整到目标屏 (sx,sy,sw,sh) 内，请求尺寸 req_w×req_h。
    跨屏 WM_DPICHANGED 会让应用自缩放（如 100%→125% 放大 1.25 倍），
    两步法：先收敛尺寸（按实测比例修正请求），再纯移位置（尺寸不动不触发重缩放），
    最后复核整窗必须在目标屏内（出屏部分 ImageGrab 截出来是黑的）。"""
    # 第一步：收敛尺寸
    for _ in range(4):
        l, t, r, b, cw, ch = get_window_rect(hwnd)
        C.windll.user32.MoveWindow(hwnd, l, t, req_w, req_h, True)
        time.sleep(0.5)
        l, t, r, b, cw, ch = get_window_rect(hwnd)
        if abs(cw - req_w) < 24 and abs(ch - req_h) < 24:
            break
        if cw > 0:
            rx = req_w / cw
            req_w = min(int(req_w * rx), sw - 8)
            req_h = min(int(req_h * rx), sh - 8)
    # 第二步：纯位置移动
    l, t, r, b, cw, ch = get_window_rect(hwnd)
    if center:
        want_l = sx + max(0, (sw - cw) // 2)
    else:
        want_l = sx + sw - cw - 16
    want_t = min(sy + max(0, (sh - ch) // 2), sy + sh - ch)
    C.windll.user32.MoveWindow(hwnd, want_l, want_t, cw, ch, True)
    time.sleep(0.4)
    # 复核
    l, t, r, b, cw, ch = get_window_rect(hwnd)
    if not (l >= sx - 8 and t >= sy - 8 and r <= sx + sw + 8 and b <= sy + sh + 8):
        C.windll.user32.MoveWindow(hwnd,
                                   max(sx, min(sx + sw - cw, l)),
                                   max(sy, min(sy + sh - ch, t)),
                                   cw, ch, True)
        time.sleep(0.3)


def park_wechat(hwnd: int):
    """停靠微信窗口：有副屏/虚拟显示器 → 停到副屏（物理屏幕完全不可见，
    ImageGrab 仍能截取）；否则退回主屏右下角小窗。"""
    # 最小化窗口 rect 在 (-32000,-32000)，MoveWindow 对它无效——必须先恢复。
    # 开机自启/托盘驻留的微信默认最小化，没这步停靠会静默失败（2026-08-18 实测）。
    if C.windll.user32.IsIconic(hwnd):
        C.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.6)
    sec = secondary_screen_rect()
    if sec:
        sx, sy, sw, sh = sec
        req_w = min(_PARK_W, sw - 8)
        req_h = min(PARK_H, sh - 8)
        # 秒退：微信会被自己的位置记忆/托盘复活弹回主屏，但每次都重新收敛
        # 会反复触发 DPI 缩放折腾窗口——已经停好就不再动
        l, t, r, b, cw, ch = get_window_rect(hwnd)
        if (l >= sx - 8 and t >= sy - 8 and r <= sx + sw + 8 and b <= sy + sh + 8
                and abs(cw - req_w) < 60):
            return
        _move_window_to(hwnd, sx, sy, sw, sh, req_w, req_h, center=True)
    else:
        pw, ph = get_screen_size()
        req_w = min(_PARK_W, pw - 80)
        req_h = min(PARK_H, ph - 120)
        _move_window_to(hwnd, 0, 0, pw, ph, req_w, req_h, center=False)


_RESTORE_W, _RESTORE_H = 1300, 1400


def restore_wechat_to_primary(hwnd: int):
    """bot 退出时把微信还回主屏：大窗居中，恢复正常使用。
    （bot 运行期间微信在虚拟屏营业，退出后归还桌面。）
    归还后自动做一次托盘复活切换（隐藏→唤出），重置 Qt 在跨屏 DPI
    往返后偶发的输入僵死（点击/关闭无响应）——2026-08-17 实测踩坑。"""
    pw, ph = get_screen_size()
    req_w = min(_RESTORE_W, pw - 80)
    req_h = min(_RESTORE_H, ph - 80)
    _move_window_to(hwnd, 0, 0, pw, ph, req_w, req_h, center=True)
    print(f"[wxmini2] WeChat restored to primary screen")
    # 输入状态重置：托盘图标完整切换优先，找不到退化为最小化/恢复
    if not revive_via_tray(hwnd):
        try:
            C.windll.user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
            time.sleep(0.5)
            C.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
            force_foreground(hwnd)
            print("[wxmini2] input state reset via min/restore fallback")
        except Exception as e:
            print(f"[wxmini2] input state reset failed: {e}")

def _grab_window(hwnd: int):
    """ImageGrab 截窗口屏幕区域。PrintWindow 对输入框等独立渲染层是盲区，
    一切"验证界面状态"的截图必须走这里（前提：窗口在前台且在屏幕内）。
    多显示器：必须 all_screens=True，否则只截主屏（副屏区域全黑）。"""
    from PIL import ImageGrab
    l, t, r, b, w, h = get_window_rect(hwnd)
    l, t, r, b = clip_to_screen(l, t, r, b)
    return ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)


def _chat_content_px(hwnd: int) -> int:
    """聊天区内容像素数（>800 视为已渲染）。"""
    import numpy as np
    img = _grab_window(hwnd)
    W, H = img.size
    if W < 100 or H < 100:
        return 0
    crop = img.crop((int(W*0.30), int(H*0.12), int(W*0.99), int(H*0.80)))
    arr = np.asarray(crop.convert("L"))
    return int((arr < 130).sum())


def _find_tray_wechat_button():
    """在任务栏托盘找微信图标按钮，返回 (x, y) 或 None。
    必须精确匹配名字（'微信'/'WeChat'）：任务栏固定/运行按钮叫
    '微信 - N 个运行窗口'，点它只是最小化/聚焦，不能触发重渲染；
    真正的托盘图标才是个开关（隐藏/唤出，用户实测可复活渲染）。
    Win11 下图标常藏在溢出弹窗（'^' chevron，独立顶层
    TopLevelWindowForOverflowXamlIsland 窗口）里——先开弹窗在弹窗内找，
    找不到再退主托盘浅搜（2026-08-18 实测修好点错图标的问题）。"""
    try:
        import uiautomation as auto
    except ImportError:
        return None

    def is_tray_icon(ctrl):
        return (ctrl.Name or '').strip() in ('微信', 'WeChat')

    def find_in(ctrl, depth):
        try:
            if is_tray_icon(ctrl):
                return ctrl
            if depth <= 0:
                return None
            for c in ctrl.GetChildren():
                hit = find_in(c, depth - 1)
                if hit is not None:
                    return hit
        except Exception:
            pass
        return None

    def center(btn):
        r = btn.BoundingRectangle
        if r.right > r.left and r.bottom > r.top:
            return (r.left + r.right) // 2, (r.top + r.bottom) // 2
        return None

    try:
        bar = auto.GetRootControl().Control(searchDepth=1, ClassName='Shell_TrayWnd')
        if not bar.Exists(1, 0.2):
            return None
        import ctypes.wintypes as wt
        U = C.windll.user32
        popups = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(h, lp):
            if U.IsWindowVisible(h):
                cls = ctypes.create_unicode_buffer(128)
                U.GetClassNameW(h, cls, 128)
                if cls.value == 'TopLevelWindowForOverflowXamlIsland':
                    popups.append(h)
            return True

        # 1) 溢出弹窗优先。chevron 是开关：弹窗已开时再点会关掉——
        #    先枚举确认弹窗不在，才点 '^' 打开
        U.EnumWindows(_cb, 0)
        if not popups:
            import pyautogui
            for chev_name in ('显示隐藏的图标', 'Notification Chevron', '更多图标'):
                chev = bar.Control(Name=chev_name, searchDepth=6)
                if chev.Exists(0.5, 0.1):
                    pos = center(chev)
                    if pos:
                        pyautogui.click(pos[0], pos[1], duration=0.08)
                        time.sleep(0.9)
                        break
            popups.clear()
            U.EnumWindows(_cb, 0)
        for h in popups:
            btn = find_in(auto.ControlFromHandle(h), 6)
            if btn is not None:
                pos = center(btn)
                if pos:
                    return pos
        # 2) 兜底：主托盘浅搜（图标直接可见时；精确名，不会误中任务栏按钮）
        btn = find_in(bar, 6)
        if btn is not None:
            pos = center(btn)
            if pos:
                return pos
    except Exception as e:
        print(f"[wxmini2] _find_tray_wechat_button error: {e}")
    return None


def revive_via_tray(hwnd: int) -> bool:
    """点任务栏托盘的微信图标复活渲染/输入僵死的窗口（用户实测有效，远轻于重启）。
    托盘图标是开关：点一下隐藏、再点一下唤出——完整切换正好重置窗口状态。"""
    pos = _find_tray_wechat_button()
    if not pos:
        print("[wxmini2] tray icon not found")
        return False
    try:
        import pyautogui
        pyautogui.click(pos[0], pos[1], duration=0.1)
        time.sleep(1.2)
        # 若窗口被隐藏，再点一次唤出（弹窗点完即关，必须重新找一次图标）
        if not C.windll.user32.IsWindowVisible(hwnd):
            pos = _find_tray_wechat_button()
            if not pos:
                print("[wxmini2] tray icon lost after hide toggle")
                return False
            pyautogui.click(pos[0], pos[1], duration=0.1)
            time.sleep(1.0)
        force_foreground(hwnd)
        time.sleep(0.5)
        return True
    except Exception as e:
        print(f"[wxmini2] revive_via_tray error: {e}")
        return False


def ensure_chat_rendered(hwnd: int, timeout: float = 8.0) -> bool:
    """微信 4.x Qt/WebView 偶发不渲染：切会话后聊天区一片空白、点击无响应。
    三级自愈：中性位置点击唤醒 → 托盘图标复活 → （调用方兜底重启微信）。
    返回聊天区是否渲染出内容。"""
    import pyautogui
    t0 = time.time()
    n = 0
    while time.time() - t0 < timeout:
        if _chat_content_px(hwnd) > 800:
            return True
        n += 1
        l, t, r, b, w, h = get_window_rect(hwnd)
        # 中性唤醒点击：聊天区中上空白处（避开气泡交互；标题栏附近也点一下）
        wake_y = 0.30 if n % 2 == 1 else 0.06
        pyautogui.click(l + int(w*0.55), t + int(h*wake_y), duration=0.08)
        time.sleep(0.9)
    if _chat_content_px(hwnd) > 800:
        return True
    # 二级：托盘图标复活
    print(f"[wxmini2] chat blank after {n} wake clicks, reviving via tray icon")
    if revive_via_tray(hwnd):
        t1 = time.time()
        while time.time() - t1 < 6.0:
            if _chat_content_px(hwnd) > 800:
                print("[wxmini2] revived via tray icon")
                return True
            time.sleep(0.8)
    return False


def _input_box_text(hwnd: int) -> str:
    """OCR 底部输入框文本区（ImageGrab）。y 85.5%~92.5%：
    上界避开聊天区最后气泡尾部，下界避开表情/发送按钮行。"""
    from PIL import Image
    img = _grab_window(hwnd)
    W, H = img.size
    crop = img.crop((int(W*0.30), int(H*0.855), int(W*0.97), int(H*0.925)))
    crop = crop.resize((int(crop.width*3), int(crop.height*3)), Image.LANCZOS)
    raw, _ = _get_ocr()(crop)
    if not raw:
        return ""
    return "".join(t for _, t, _ in sorted(raw, key=lambda r: r[0][0][1]))


def _chat_tail_text(hwnd: int) -> str:
    """OCR 聊天区底部（ImageGrab，y 58%~84%）。调试/兜底用；
    发送成功的正式判定走数据库（_send_text_core）。"""
    from PIL import Image
    img = _grab_window(hwnd)
    W, H = img.size
    crop = img.crop((int(W*0.29), int(H*0.58), int(W*0.99), int(H*0.84)))
    crop = crop.resize((crop.width*2, crop.height*2), Image.LANCZOS)
    raw, _ = _get_ocr()(crop)
    if not raw:
        return ""
    return " ".join(t for _, t, _ in sorted(raw, key=lambda r: r[0][0][1]))


def _fuzzy_contains(needle: str, hay: str, threshold: float = 0.62) -> bool:
    """滑窗模糊包含：小窗口 OCR 误字率高（叫→啪、廖→膝），
    精确子串匹配不现实，用局部相似度判定。"""
    from difflib import SequenceMatcher
    needle = (needle or "").strip()
    hay = (hay or "").strip()
    if not needle or not hay:
        return False
    if needle in hay:
        return True
    n = len(needle)
    best = 0.0
    for i in range(0, max(1, len(hay) - n // 2)):
        r = SequenceMatcher(None, needle, hay[i:i + n + 2]).ratio()
        if r > best:
            best = r
            if best >= threshold:
                return True
    return False


def send_text(contact: str, text: str, wait_idle: bool = True, open_fn=None) -> bool:
    """打开会话并发送文字（用户友好包装）。
    - 等用户键鼠空闲再动手（避免抢输入，最多等 2 分钟后强制发）
    - 窗口停靠右下角（不占主工作区）
    - 发完把前台焦点还给用户原来的窗口
    成功判定以「聊天区出现该消息气泡」为准（见 _send_text_core）。
    open_fn(hwnd, contact, timeout) -> bool：可注入的会话打开器（wxapi 固定坐标
    快路径用），默认 open_chat_by_click（OCR 扫描）。"""
    if wait_idle and not wait_user_idle():
        print("[wxmini2] user still active, sending anyway (timeout)")
    hwnd = find_wechat()
    park_wechat(hwnd)
    prev_fg = C.windll.user32.GetForegroundWindow()
    try:
        return _send_text_core(hwnd, contact, text, open_fn=open_fn)
    finally:
        if prev_fg and prev_fg != hwnd:
            try:
                C.windll.user32.SetForegroundWindow(prev_fg)
            except Exception:
                pass


def _ensure_fg(hwnd: int):
    """截图判读前保证微信在前台：被其他窗口遮挡时 ImageGrab 截到的是
    别的窗口，像素/OCR 判读全错，会引发误清理草稿等连锁问题。"""
    try:
        if C.windll.user32.GetForegroundWindow() != hwnd:
            force_foreground(hwnd)
            time.sleep(0.25)
    except Exception:
        pass


def _input_dark_px(hwnd: int) -> int:
    """输入框文本区暗像素数。粘贴/清空的判定用像素而不是 OCR 文字：
    小窗口 OCR 误字率高，但"有没有字"的像素证据很稳。"""
    import numpy as np
    _ensure_fg(hwnd)
    img = _grab_window(hwnd)
    W, H = img.size
    if W < 100:
        return 0
    crop = img.crop((int(W*0.30), int(H*0.855), int(W*0.97), int(H*0.925)))
    arr = np.asarray(crop.convert("L"))
    return int((arr < 140).sum())


def _send_text_core(hwnd: int, contact: str, text: str, open_fn=None) -> bool:
    gap = time.time() - _last_send_ts[0]
    if gap < _MIN_SEND_GAP_S:
        time.sleep(_MIN_SEND_GAP_S - gap)
    force_foreground(hwnd)
    opener = open_fn or open_chat_by_click
    # 切到指定会话
    if not opener(hwnd, contact, timeout=6.0):
        print(f"[wxmini2] open_chat failed: {contact}")
        return False
    time.sleep(0.3)
    # Qt 渲染兜底：聊天区空白时强制唤醒（点击→托盘复活→重启微信），
    # 避免点击/键盘全部落空
    if not ensure_chat_rendered(hwnd):
        print("[wxmini2] render dead, escalating to WeChat restart")
        try:
            hwnd = restart_wechat()
            if not opener(hwnd, contact, timeout=6.0):
                return False
            if not ensure_chat_rendered(hwnd):
                return False
        except Exception as e:
            print("[wxmini2] restart_wechat failed:", e)
            return False
    target_username = (_LAST_OPENED[0] or (None, None))[1]
    import pyautogui, pyperclip
    l, t, r, b, w, h = get_window_rect(hwnd)
    # 点击输入框文本区中部（y=0.89，避开底部按钮行；之前的 0.92 会点偏）
    for attempt in range(3):
        # 偶发焦点丢失：每次重试前重新置前（虚拟屏上点击可能被时序问题吃掉）
        force_foreground(hwnd)
        in_x = l + int(w * 0.55)
        in_y = t + int(h * (0.89 - attempt * 0.02))
        pyautogui.click(in_x, in_y, duration=0.1)
        time.sleep(0.3)
        # 清掉可能残留的草稿
        pyautogui.hotkey('ctrl', 'a', interval=0.03)
        pyautogui.press('delete')
        time.sleep(0.2)
        base = _input_dark_px(hwnd)
        # 粘贴
        pyperclip.copy(text)
        time.sleep(0.1)
        pyautogui.hotkey('ctrl', 'v', interval=0.05)
        time.sleep(0.5)
        # 粘贴判定（像素）：输入框暗像素显著增加 = 文字已进框
        # 阈值随文本长度缩放（虚拟屏 100% 缩放下文字暗像素比主屏 125% 少，
        # 阈值按低线标定：2 个字 ~44px，长消息 200px 封顶）
        after = _input_dark_px(hwnd)
        need = max(40, min(200, 40 * len(re.sub(r"\s", "", text))))
        if after > base + 40 or after > need:
            break
        print(f"[wxmini2] paste verify fail (attempt {attempt+1}): dark px {base} -> {after} (need {need})")
    else:
        print("[wxmini2] paste failed 3 times, abort send (draft kept)")
        return False
    # 发送：Enter → Ctrl+Enter（兼容"按 Ctrl+Enter 发送"设置）→ 点「发送」按钮。
    # 失败不删草稿：保留文本便于下轮重试/人工补救（用户可见"全选又删掉"的老行为已移除）。
    sent_action = False
    for method in ("enter", "ctrlenter", "btn"):
        if method == "enter":
            pyautogui.press('enter')
        elif method == "ctrlenter":
            pyautogui.hotkey('ctrl', 'enter', interval=0.05)
        else:
            # 重新聚焦输入框再点按钮（防前面按键后焦点漂移）
            pyautogui.click(l + int(w * 0.55), t + int(h * 0.89), duration=0.08)
            time.sleep(0.3)
            btn = _find_send_button(hwnd)
            if not btn:
                continue
            pyautogui.click(btn[0], btn[1], duration=0.1)
        time.sleep(1.2)
        if _input_dark_px(hwnd) < 120:
            sent_action = True
            break
        print(f"[wxmini2] send via {method} didn't clear input, trying next")
    if not sent_action:
        print("[wxmini2] send failed: input not cleared (draft kept, not deleted)")
        return False
    _last_send_ts[0] = time.time()
    # 硬验证：数据库出现这条消息（文本精确匹配，零 OCR 依赖）
    if target_username:
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                msgs = _get_db().get_messages(target_username, limit=5)
            except Exception:
                msgs = []
            for m in msgs:
                if (m.get("content") or "").strip() == text.strip():
                    return True
            time.sleep(1.2)
        print("[wxmini2] send not confirmed in DB within 30s")
        return False
    return True


def _input_box_text_stripped(hwnd: int) -> str:
    """输入框文本（去掉「发送」按钮等噪声词后）"""
    t = _input_box_text(hwnd)
    for noise in ("发送",):
        t = t.replace(noise, "")
    return t.strip()


def _find_send_button(hwnd: int):
    """OCR 找「发送」按钮的屏幕坐标（Enter 失败时的兜底点击）。
    bbox 是 crop 内坐标，换算窗口比例前必须加回 crop 原点偏移
    （旧版漏了这步，算出的按钮位置 y 永远偏低 0.80*H）。"""
    _ensure_fg(hwnd)
    img = _grab_window(hwnd)
    W, H = img.size
    l, t, r, b, w, h = get_window_rect(hwnd)
    cx0, cy0 = int(W * 0.28), int(H * 0.80)
    crop = img.crop((cx0, cy0, W, H))
    raw, _ = _get_ocr()(crop)
    if not raw:
        return None
    for bbox, text, conf in raw:
        if "发送" in text and conf > 0.6:
            cx = (bbox[0][0] + bbox[2][0]) / 2 + cx0
            cy = (bbox[0][1] + bbox[2][1]) / 2 + cy0
            return int(l + cx / W * w), int(t + cy / H * h)
    return None

# ============== 其它函数（占位 stub）=============

def send_text_at(contact: str, text: str, at_user: str) -> bool:
    """@某人发消息。先发普通消息，再@。简单实现：发消息，附加 [AT]标记。"""
    return send_text(contact, f"{text}\n@{at_user}")

def _set_clipboard_image(path: str) -> bool:
    """图片文件 → 剪贴板 CF_DIB（PIL 存 BMP 再去掉 14 字节文件头；
    带 alpha 的先贴白底，否则 CF_DIB 按 BI_RGB 解读会变黑底）。"""
    import io
    try:
        import win32clipboard as wc
        from PIL import Image
        img = Image.open(path)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        with io.BytesIO() as out:
            img.save(out, "BMP")
            dib = out.getvalue()[14:]
        wc.OpenClipboard()
        try:
            wc.EmptyClipboard()
            wc.SetClipboardData(wc.CF_DIB, dib)
        finally:
            wc.CloseClipboard()
        return True
    except Exception as e:
        print("[wxmini2] set clipboard image failed:", e)
        return False


def _wait_image_in_db(username: Optional[str], since_ts: float, timeout_s: float = 30.0) -> bool:
    """轮询 DB 等 own 侧出现 since_ts 之后的图片消息（发送硬确认）。"""
    if not username:
        return False
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            msgs = read_chat_db(username, limit=5)
        except Exception:
            msgs = []
        for m in msgs:
            if (m.get("side") == "own" and m.get("kind") == "image"
                    and float(m.get("ts") or 0) >= since_ts - 2):
                return True
        time.sleep(1.5)
    return False


def _set_clipboard_files(paths) -> bool:
    """文件列表 → 剪贴板 CF_HDROP（DROPFILES 头 + 双 \0 结尾的宽字符路径表）。"""
    import struct
    try:
        import win32clipboard as wc
        files = ("\0".join(paths) + "\0\0").encode("utf-16-le")
        df = struct.pack("IiiII", 20, 0, 0, 0, 1)  # DROPFILES: pFiles=20, pt(0,0), fNC=0, fWide=1
        wc.OpenClipboard()
        try:
            wc.EmptyClipboard()
            wc.SetClipboardData(wc.CF_HDROP, df + files)
        finally:
            wc.CloseClipboard()
        return True
    except Exception as e:
        print("[wxmini2] set clipboard files failed:", e)
        return False


def _wait_file_in_db(username: Optional[str], since_ts: float, timeout_s: float = 30.0) -> bool:
    """轮询 DB 等 own 侧出现 since_ts 之后的文件消息（发送硬确认）。"""
    if not username:
        return False
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            msgs = read_chat_db(username, limit=5)
        except Exception:
            msgs = []
        for m in msgs:
            if (m.get("side") == "own" and m.get("kind") == "file"
                    and float(m.get("ts") or 0) >= since_ts - 2):
                return True
        time.sleep(1.5)
    return False


def send_file(contact: str, path: str) -> bool:
    """发任意文件（CF_HDROP 剪贴板粘贴，与发图同一条稳定链）。
    mp3/wav 等音频在微信里以可内联播放的文件卡片呈现——PC 端没有原生语音气泡，
    这是 bot 发语音的实际形态。"""
    if not os.path.exists(path):
        print(f"[wxmini2] send_file: file not found {path}")
        return False
    if not wait_user_idle():
        print("[wxmini2] user still active, sending anyway (timeout)")
    hwnd = find_wechat()
    park_wechat(hwnd)
    prev_fg = C.windll.user32.GetForegroundWindow()
    try:
        return _send_file_core(hwnd, contact, path)
    finally:
        if prev_fg and prev_fg != hwnd:
            try:
                C.windll.user32.SetForegroundWindow(prev_fg)
            except Exception:
                pass


def _send_file_core(hwnd: int, contact: str, path: str) -> bool:
    import pyautogui
    gap = time.time() - _last_send_ts[0]
    if gap < _MIN_SEND_GAP_S:
        time.sleep(_MIN_SEND_GAP_S - gap)
    force_foreground(hwnd)
    if not open_chat_by_click(hwnd, contact, timeout=6.0):
        print(f"[wxmini2] open_chat failed: {contact}")
        return False
    time.sleep(0.3)
    if not ensure_chat_rendered(hwnd):
        print("[wxmini2] render dead, escalating to WeChat restart")
        try:
            hwnd = restart_wechat()
            if not open_chat_by_click(hwnd, contact, timeout=6.0):
                return False
            if not ensure_chat_rendered(hwnd):
                return False
        except Exception as e:
            print("[wxmini2] restart_wechat failed:", e)
            return False
    target_username = (_LAST_OPENED[0] or (None, None))[1]
    l, t, r, b, w, h = get_window_rect(hwnd)
    # 1. 点输入框并确认无残留草稿（防 Enter 误发旧内容）
    force_foreground(hwnd)
    in_x = l + int(w * 0.55)
    in_y = t + int(h * 0.89)
    pyautogui.click(in_x, in_y, duration=0.1)
    time.sleep(0.25)
    draft = _input_box_text_stripped(hwnd)
    if draft:
        print(f"[wxmini2] send_file abort: input has draft {draft[:20]!r}")
        return False
    # 2. 文件上剪贴板 → 粘贴（文件卡片预览约 1s）
    if not _set_clipboard_files([os.path.abspath(path)]):
        return False
    pyautogui.hotkey('ctrl', 'v', interval=0.05)
    time.sleep(1.2)
    # 3. 发送：Enter，未确认再 Ctrl+Enter；DB 出现 own 文件消息才算成
    t0 = time.time()
    sent = False
    for method in ('enter', 'ctrl_enter'):
        if method == 'enter':
            pyautogui.press('enter')
        else:
            pyautogui.hotkey('ctrl', 'enter')
        time.sleep(1.0)
        if _wait_file_in_db(target_username, t0, timeout_s=20.0):
            sent = True
            break
        print(f"[wxmini2] send_file via {method} not confirmed, trying next")
    if sent:
        _last_send_ts[0] = time.time()
    else:
        print("[wxmini2] send_file not confirmed in DB")
    return sent


def send_image(contact: str, path: str) -> bool:
    """发图片：CF_DIB 上剪贴板 → 点输入框 → Ctrl+V → Enter → DB 确认图片消息。
    走剪贴板粘贴而不是「+」→文件对话框：对话框是独立渲染面，视觉定位易碎；
    粘贴路径只依赖输入框焦点，和发文本同一条稳定链。"""
    if not os.path.exists(path):
        print(f"[wxmini2] send_image: file not found {path}")
        return False
    if not wait_user_idle():
        print("[wxmini2] user still active, sending anyway (timeout)")
    hwnd = find_wechat()
    park_wechat(hwnd)
    prev_fg = C.windll.user32.GetForegroundWindow()
    try:
        return _send_image_core(hwnd, contact, path)
    finally:
        if prev_fg and prev_fg != hwnd:
            try:
                C.windll.user32.SetForegroundWindow(prev_fg)
            except Exception:
                pass


def _send_image_core(hwnd: int, contact: str, path: str) -> bool:
    import pyautogui
    gap = time.time() - _last_send_ts[0]
    if gap < _MIN_SEND_GAP_S:
        time.sleep(_MIN_SEND_GAP_S - gap)
    force_foreground(hwnd)
    if not open_chat_by_click(hwnd, contact, timeout=6.0):
        print(f"[wxmini2] open_chat failed: {contact}")
        return False
    time.sleep(0.3)
    if not ensure_chat_rendered(hwnd):
        print("[wxmini2] render dead, escalating to WeChat restart")
        try:
            hwnd = restart_wechat()
            if not open_chat_by_click(hwnd, contact, timeout=6.0):
                return False
            if not ensure_chat_rendered(hwnd):
                return False
        except Exception as e:
            print("[wxmini2] restart_wechat failed:", e)
            return False
    target_username = (_LAST_OPENED[0] or (None, None))[1]
    l, t, r, b, w, h = get_window_rect(hwnd)
    # 1. 点输入框并确认无残留草稿（防 Enter 误发旧文本/旧图）
    force_foreground(hwnd)
    in_x = l + int(w * 0.55)
    in_y = t + int(h * 0.89)
    pyautogui.click(in_x, in_y, duration=0.1)
    time.sleep(0.25)
    draft = _input_box_text_stripped(hwnd)
    if draft:
        print(f"[wxmini2] send_image abort: input has draft {draft[:20]!r}")
        return False
    # 2. 图片上剪贴板 → 粘贴（缩略图渲染约 1s）
    if not _set_clipboard_image(path):
        return False
    pyautogui.hotkey('ctrl', 'v', interval=0.05)
    time.sleep(1.2)
    # 3. 发送：Enter，未确认再 Ctrl+Enter；DB 出现 own 图片消息才算成
    t0 = time.time()
    sent = False
    for method in ('enter', 'ctrl_enter'):
        if method == 'enter':
            pyautogui.press('enter')
        else:
            pyautogui.hotkey('ctrl', 'enter')
        time.sleep(1.0)
        if _wait_image_in_db(target_username, t0, timeout_s=20.0):
            sent = True
            break
        print(f"[wxmini2] send_image via {method} not confirmed, trying next")
    if sent:
        _last_send_ts[0] = time.time()
    else:
        print("[wxmini2] send_image not confirmed in DB")
    return sent

def send_emoji(contact: str, name: str) -> bool:
    """发微信自带 emoji（暂未实现）"""
    print("[wxmini2] send_emoji: 暂未实现")
    return False

def send_sticker(contact: str, name: str) -> bool:
    """发收藏贴纸（暂未实现）"""
    print("[wxmini2] send_sticker: 暂未实现")
    return False

def quote_reply(hwnd: int, text: str) -> bool:
    """引用回复（暂未实现：需要右键消息气泡）"""
    print("[wxmini2] quote_reply: 暂未实现")
    return False

SIDEBAR_ALIVE_TH = 2000   # sidebar_alive_px 活性阈值：健康 26k~40k，死/登录页 <200


def sidebar_alive_px(hwnd: int) -> int:
    """侧边栏"活性"分值：深色像素数 × 行起伏度。已登录主界面有头像+会话
    文字（行剖面起伏大）；扫码/确认登录页/渲染死侧边栏空白或只剩边框线
    （2026-08-18 实测：纯边框线就有 ~7800 暗像素，单纯计数会误判已登录）。
    返回 dark*min(1, rowstd/10)，>SIDEBAR_ALIVE_TH 视为活着。"""
    import numpy as np
    try:
        _ensure_fg(hwnd)
        img = _grab_window(hwnd)
        W, H = img.size
        if W < 100:
            return 0
        crop = img.crop((0, int(H * LIST_Y1), int(W * LIST_X2), int(H * LIST_Y2)))
        arr = np.asarray(crop.convert("L"))
        dark = int((arr < 160).sum())
        rowstd = float((arr < 160).sum(axis=1).std())
        return int(dark * min(1.0, rowstd / 10.0))
    except Exception:
        return 0


def _handle_login_page(hwnd: int, timeout: float = 20.0) -> bool:
    """重启后若停在登录页：确认页（头像+「进入微信」按钮）点按钮自动登录；
    扫码页无法自动化，返回 False。登录成功判定 = 侧边栏出现内容。"""
    import pyautogui
    t0 = time.time()
    while time.time() - t0 < timeout:
        if sidebar_alive_px(hwnd) > SIDEBAR_ALIVE_TH:
            return True
        l, t, r, b, w, h = get_window_rect(hwnd)
        # 「进入微信」按钮在页面中下部；扫码页点上无害
        pyautogui.click(l + w // 2, t + int(h * 0.78), duration=0.1)
        time.sleep(3.0)
    return sidebar_alive_px(hwnd) > SIDEBAR_ALIVE_TH


def _find_wechat_exe() -> str:
    import glob
    exe = r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"
    if not os.path.exists(exe):
        hits = glob.glob(r"C:\Program Files*\Tencent\Weixin\Weixin.exe")
        if not hits:
            raise RuntimeError("Weixin.exe not found")
        exe = hits[0]
    return exe


def _wait_main_window(timeout: float) -> int:
    """等一个可见非最小化的微信主窗口出现，返回 hwnd 或 0。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2.0)
        try:
            wins = _enum_wechat_windows()
        except Exception:
            wins = []
        for h, vis, _ in wins:
            if vis and not ctypes.windll.user32.IsIconic(h):
                return h
    return 0


def restart_wechat() -> int:
    """重启微信：渲染进程挂死（整窗空白、点击无效）时的自愈手段。
    **优雅退出优先**：对主窗口发 WM_CLOSE 再启动新实例——保住登录态
    （taskkill /F 会破坏会话文件触发扫码验证，2026-08-18 实测；
    优雅关闭后新实例直接进主界面，子进程残留无妨）。
    优雅路径 60s 没起窗才退回强杀重来。登录确认页自动点「进入微信」，
    扫码页无法自动化则抛错。返回新 hwnd。"""
    import subprocess
    exe = _find_wechat_exe()
    print("[wxmini2] restarting WeChat (graceful close first)...")
    try:
        for h, vis, _ in _enum_wechat_windows():
            if vis:
                C.windll.user32.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE
    except Exception:
        pass
    time.sleep(3.0)
    subprocess.Popen([exe], cwd=os.path.dirname(exe))
    hwnd = _wait_main_window(60)
    if not hwnd:
        # 优雅路径失败 → 强杀重来（最后手段，可能触发重新登录）
        print("[wxmini2] graceful path failed, falling back to taskkill")
        subprocess.run(["taskkill", "/F", "/IM", "Weixin.exe"], capture_output=True)
        time.sleep(2.0)
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
        hwnd = _wait_main_window(90)
    if not hwnd:
        raise RuntimeError("WeChat window did not come back (login needed?)")
    # 等窗口渲染出内容（登录页/主界面都算）
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            img = _grab_window(hwnd)
            import numpy as np
            arr = np.asarray(img.convert("L"))
            if int((arr < 130).sum()) > 3000:
                break
        except Exception:
            pass
        time.sleep(2.0)
    else:
        print("[wxmini2] window up but content not rendering yet")
    # 登录页处理：多次强杀后微信常要求确认登录（2026-08-18 实测）
    if sidebar_alive_px(hwnd) < SIDEBAR_ALIVE_TH:
        print("[wxmini2] login page detected, trying auto-confirm...")
        if not _handle_login_page(hwnd):
            raise RuntimeError("WeChat is on QR-login page; manual scan required")
    print(f"[wxmini2] WeChat restarted, hwnd={hwnd}")
    return hwnd

# ============== 健康检查 ==============

if __name__ == "__main__":
    print("=== wxmini2 visual version self-test ===")
    hwnd = find_wechat()
    print(f"found wechat: hwnd={hwnd}")
    l, t, r, b, w, h = get_window_rect(hwnd)
    print(f"window rect: {w}x{h} at ({l},{t})")
    img = pw_shot(hwnd)
    print(f"screenshot: {img.size}")
    img.save(r"E:\vibe_coding_project\ALYOSHKA\bot-auto-reply\_visual_test.png")
    print("saved _visual_test.png")
    print()
    print("--- sessions ---")
    for s in list_sessions(hwnd, retries=2):
        print(f"  {s['name']!r}  last={s['last']!r}  conf={s['conf']:.2f}")
    print()
    print("--- current chat ---")
    print(f"  name: {current_chat_name(hwnd)}")
    print()
    print("--- read chat ---")
    for m in read_chat(hwnd):
        print(f"  [{m['side']:5s}] {m['kind']:5s}  {m['text']!r}")
