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


def ensure_window_in_screen(hwnd: int):
    """如果微信窗口超出屏幕（部分在屏幕外），自动 MoveWindow 移回屏幕内。
    ImageGrab 截屏方案的关键：窗口必须在屏幕内可见。"""
    l, t, r, b, w, h = get_window_rect(hwnd)
    vx, vy, vw, vh = get_virtual_screen()
    if l >= vx and t >= vy and r <= vx + vw and b <= vy + vh:
        return
    new_l = max(vx, min(vx + vw - 100, l)) if l < vx else min(vx + vw - 100, l)
    new_t = max(vy, min(vy + vh - 100, t)) if t < vy else min(vy + vh - 100, t)
    new_w = min(w, int(vw * 0.8))
    new_h = min(h, int(vh * 0.8))
    print(f"[wxmini2] ensure_window_in_screen: ({l},{t})-({r},{b}) -> ({new_l},{new_t}) {new_w}x{new_h}")
    C.windll.user32.MoveWindow(hwnd, new_l, new_t, new_w, new_h, True)
    time.sleep(0.3)

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


def _get_db():
    """懒加载 WeChatDB 单例（首次初始化约 6 秒）"""
    if _WECHAT_DB[0] is None:
        from wechatauto import WeChatDB
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
    filehelper 探测的自 sid 兜底。群消息内容剥离 'wxid_xxx:\n' 前缀。"""
    db = _get_db()
    try:
        msgs = db.get_messages(username, limit=limit)
    except Exception as e:
        print(f"[wxmini2] read_chat_db error: {e}")
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
    找含 fingerprint 的行，返回 (窗口内像素 x, y) 或 None。"""
    from PIL import Image
    img = _grab_window(hwnd)
    W, H = img.size
    crop = _crop_pct(img, LIST_X1, LIST_Y1, LIST_X2, LIST_Y2)
    crop2 = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    raw, _ = _get_ocr()(crop2)
    if not raw:
        return None
    CH, CW = crop.height, crop.width
    for bbox, text, conf in raw:
        if conf < 0.5 or not text:
            continue
        cy = (bbox[0][1] + bbox[2][1]) / 2 / 2 / CH
        cx = (bbox[0][0] + bbox[2][0]) / 2 / 2 / CW
        if fingerprint and fingerprint in text:
            px = int((LIST_X1 + (LIST_X2 - LIST_X1) * min(max(cx, 0.1), 0.9)) * W)
            py = int((LIST_Y1 + (LIST_Y2 - LIST_Y1) * min(max(cy, 0.0), 1.0)) * H)
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


def park_wechat(hwnd: int):
    """停靠微信窗口：有副屏/虚拟显示器 → 停到副屏（物理屏幕完全不可见，
    ImageGrab 仍能截取）；否则退回主屏右下角小窗。
    跨屏会触发 WM_DPICHANGED（应用自缩放，如 125%→100% 即 ×0.8），尺寸会跳；
    用收敛循环处理：移动→实测→按比例修正请求→钳制到目标屏矩形内。"""
    sec = secondary_screen_rect()
    if sec:
        sx, sy, sw, sh = sec
        req_w = min(_PARK_W, sw - 8)
        req_h = min(PARK_H, sh - 8)
    else:
        pw, ph = get_screen_size()
        sx, sy, sw, sh = 0, 0, pw, ph
        req_w = min(_PARK_W, sw - 80)
        req_h = min(PARK_H, sh - 120)

    # 第一步：收敛尺寸（跨屏 WM_DPICHANGED 会让应用自缩放；按实测比例修正请求）
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
    # 第二步：纯位置移动（尺寸不变 → 不触发 DPI 重缩放，位置才能站住）
    l, t, r, b, cw, ch = get_window_rect(hwnd)
    if sec:
        want_l = sx + max(0, (sw - cw) // 2)
    else:
        want_l = sx + sw - cw - 16
    want_t = min(sy + max(0, (sh - ch) // 2), sy + sh - ch)
    C.windll.user32.MoveWindow(hwnd, want_l, want_t, cw, ch, True)
    time.sleep(0.4)
    # 复核：整窗必须在目标屏内（掉出屏外的部分 ImageGrab 截出来是黑的）
    l, t, r, b, cw, ch = get_window_rect(hwnd)
    if not (l >= sx - 8 and t >= sy - 8 and r <= sx + sw + 8 and b <= sy + sh + 8):
        C.windll.user32.MoveWindow(hwnd,
                                   max(sx, min(sx + sw - cw, l)),
                                   max(sy, min(sy + sh - ch, t)),
                                   cw, ch, True)
        time.sleep(0.3)

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


def revive_via_tray(hwnd: int) -> bool:
    """点任务栏托盘的微信图标复活渲染挂死的窗口（用户实测有效，远轻于重启）。"""
    try:
        import uiautomation as auto
    except ImportError:
        return False
    try:
        bar = auto.GetRootControl().Control(searchDepth=1, ClassName='Shell_TrayWnd')
        if not bar.Exists(1, 0.2):
            return False

        def walk(ctrl, depth):
            if depth <= 0:
                return None
            for c in ctrl.GetChildren():
                name = c.Name or ''
                if ('微信' in name) or ('WeChat' in name):
                    return c
                hit = walk(c, depth - 1)
                if hit is not None:
                    return hit
            return None

        btn = walk(bar, 4)
        if btn is None:
            print("[wxmini2] tray icon not found")
            return False
        r = btn.BoundingRectangle
        if r.right <= r.left or r.bottom <= r.top:
            return False
        x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        import pyautogui
        pyautogui.click(x, y, duration=0.1)
        time.sleep(1.2)
        # 托盘图标是开关：若窗口反而被隐藏，再点一次唤出
        if not C.windll.user32.IsWindowVisible(hwnd):
            pyautogui.click(x, y, duration=0.1)
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


def send_text(contact: str, text: str) -> bool:
    """打开会话并发送文字（用户友好包装）。
    - 等用户键鼠空闲再动手（避免抢输入，最多等 2 分钟后强制发）
    - 窗口停靠右下角（不占主工作区）
    - 发完把前台焦点还给用户原来的窗口
    成功判定以「聊天区出现该消息气泡」为准（见 _send_text_core）。
    """
    if not wait_user_idle():
        print("[wxmini2] user still active, sending anyway (timeout)")
    hwnd = find_wechat()
    park_wechat(hwnd)
    prev_fg = C.windll.user32.GetForegroundWindow()
    try:
        return _send_text_core(hwnd, contact, text)
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


def _send_text_core(hwnd: int, contact: str, text: str) -> bool:
    gap = time.time() - _last_send_ts[0]
    if gap < _MIN_SEND_GAP_S:
        time.sleep(_MIN_SEND_GAP_S - gap)
    force_foreground(hwnd)
    # 切到指定会话
    if not open_chat_by_click(hwnd, contact, timeout=6.0):
        print(f"[wxmini2] open_chat_by_click failed: {contact}")
        return False
    time.sleep(0.3)
    # Qt 渲染兜底：聊天区空白时强制唤醒（点击→托盘复活→重启微信），
    # 避免点击/键盘全部落空
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
    import pyautogui, pyperclip
    l, t, r, b, w, h = get_window_rect(hwnd)
    # 点击输入框文本区中部（y=0.89，避开底部按钮行；之前的 0.92 会点偏）
    for attempt in range(3):
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
        # 阈值随文本长度缩放（短消息 2 个字只有 ~200 暗像素，固定阈值会误杀）
        after = _input_dark_px(hwnd)
        need = max(80, min(250, 60 * len(re.sub(r"\s", "", text))))
        if after > base + 60 and after > need:
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
            time.sleep(3)
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
    """OCR 找「发送」按钮的屏幕坐标（Enter 失败时的兜底点击）"""
    _ensure_fg(hwnd)
    img = _grab_window(hwnd)
    W, H = img.size
    l, t, r, b, w, h = get_window_rect(hwnd)
    crop = img.crop((int(W*0.28), int(H*0.80), W, H))
    raw, _ = _get_ocr()(crop)
    if not raw:
        return None
    for bbox, text, conf in raw:
        if "发送" in text and conf > 0.6:
            cx = (bbox[0][0] + bbox[2][0]) / 2 / W
            cy = (bbox[0][1] + bbox[2][1]) / 2 / H
            return int(l + cx * w), int(t + cy * h)
    return None

# ============== 其它函数（占位 stub）=============

def send_text_at(contact: str, text: str, at_user: str) -> bool:
    """@某人发消息。先发普通消息，再@。简单实现：发消息，附加 [AT]标记。"""
    return send_text(contact, f"{text}\n@{at_user}")

def send_image(contact: str, path: str) -> bool:
    """发图片（暂未实现：需要打开图片按钮、文件对话框、确认）"""
    print("[wxmini2] send_image: 暂未实现（视觉方案需点文件对话框）")
    return False

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

def restart_wechat() -> int:
    """重启微信：渲染进程挂死（整窗空白、点击无效）时的自愈手段。
    强杀 Weixin.exe → 重新启动 → 等主窗口出现且有内容。返回新 hwnd。"""
    import subprocess, glob
    print("[wxmini2] restarting WeChat...")
    subprocess.run(["taskkill", "/F", "/IM", "Weixin.exe"],
                   capture_output=True)
    time.sleep(2.0)
    exe = r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"
    if not os.path.exists(exe):
        hits = glob.glob(r"C:\Program Files*\Tencent\Weixin\Weixin.exe")
        if not hits:
            raise RuntimeError("Weixin.exe not found")
        exe = hits[0]
    subprocess.Popen([exe], cwd=os.path.dirname(exe))
    # 等主窗口
    deadline = time.time() + 90
    hwnd = 0
    while time.time() < deadline:
        time.sleep(2.0)
        try:
            wins = _enum_wechat_windows()
        except Exception:
            wins = []
        for h, vis, _ in wins:
            if vis and not ctypes.windll.user32.IsIconic(h):
                hwnd = h
                break
        if hwnd:
            break
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
                print(f"[wxmini2] WeChat restarted, hwnd={hwnd}")
                return hwnd
        except Exception:
            pass
        time.sleep(2.0)
    print("[wxmini2] window up but content not rendering yet")
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
