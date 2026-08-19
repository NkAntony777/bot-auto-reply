# -*- coding: utf-8 -*-
"""wxmini2: WeChat 4.1.12 PC automation — read + send via UIA.

Extends the hand-rolled UIA approach (no wxauto, direct UIA walking):
  list_sessions()      -> read left sidebar conversation list
  read_chat()          -> read current chat message list (visible bubble items)
  send_text(contact,t) -> open chat by search + type + Enter (verified)

Verified 2026-08-11: session_list id='session_list' (13 convs incl. groups),
chat_message_list id='chat_message_list' exposes bubble items with Name = text.
"""
import os, sys, time, ctypes, random
import ctypes.wintypes as wt
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

u = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# ---------------------------------------------------------------- input
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_ulonglong)]

class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]

def type_unicode(text, delay=0.03):
    for ch in text:
        code = ord(ch)
        for flags in (0x0004, 0x0004 | 0x0002):
            inp = INPUT(); inp.type = 1
            inp.u.ki = KEYBDINPUT(0, code, flags, 0, 0)
            if u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
                raise RuntimeError("SendInput failed")
        time.sleep(delay)

def key(vk):
    u.keybd_event(vk, 0, 0, 0); time.sleep(0.04); u.keybd_event(vk, 0, 2, 0)

# ---------------------------------------------------------------- clipboard paste
# 64-bit pointer fix: GlobalAlloc/GlobalLock return pointers
k32.GlobalAlloc.restype = ctypes.c_void_p
k32.GlobalLock.restype = ctypes.c_void_p
k32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
k32.GlobalLock.argtypes = [ctypes.c_void_p]
k32.GlobalUnlock.argtypes = [ctypes.c_void_p]

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

u.GetClipboardData.restype = ctypes.c_void_p
u.GetClipboardData.argtypes = [wt.UINT]
u.SetClipboardData.restype = ctypes.c_void_p
u.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]

def get_clipboard_text():
    """Read current clipboard as text (for verification). None = open failed."""
    for _ in range(5):
        if u.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        return None
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = k32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            k32.GlobalUnlock(h)
    finally:
        u.CloseClipboard()

def set_clipboard(text):
    """Put text on the Windows clipboard as CF_UNICODETEXT."""
    data = (text + "\0").encode("utf-16-le")
    for _ in range(5):
        if u.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("OpenClipboard failed")
    try:
        u.EmptyClipboard()
        h = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise RuntimeError("GlobalAlloc failed")
        ptr = k32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(h)
        if not u.SetClipboardData(CF_UNICODETEXT, h):
            raise RuntimeError("SetClipboardData failed")
    finally:
        u.CloseClipboard()

def _ctrl_combo(vk):
    """Ctrl+<vk> chord."""
    u.keybd_event(0x11, 0, 0, 0); time.sleep(0.03)
    u.keybd_event(vk, 0, 0, 0); time.sleep(0.03)
    u.keybd_event(vk, 0, 2, 0); time.sleep(0.03)
    u.keybd_event(0x11, 0, 2, 0)

def paste():
    """Send Ctrl+V."""
    _ctrl_combo(0x56)

def select_all():
    _ctrl_combo(0x41)

def copy_selection():
    _ctrl_combo(0x43)

_PASTE_SENTINEL = "\x00PASTE_CHECK\x00"

def paste_verified(text, allow_suffix=False):
    """真实粘贴并强制验证：剪贴板写入→读回校验→Ctrl+V→全选→复制→读回，
    确认输入框里真的有这段文字（不是注入、也不是静默失败）。Returns True/False。
    allow_suffix=True 用于输入框已有 @ 标签的场景：读回以 text 结尾即算成功。"""
    set_clipboard(text)
    if get_clipboard_text() != text:
        print("!! clipboard write verify failed")
        return False
    paste()
    time.sleep(random.uniform(0.45, 0.8))
    # 用哨兵清空剪贴板：若输入框为空，Ctrl+C 不会改剪贴板，读回仍是哨兵 → 检出
    set_clipboard(_PASTE_SENTINEL)
    select_all(); time.sleep(0.15)
    copy_selection(); time.sleep(random.uniform(0.35, 0.6))
    got = get_clipboard_text() or ""
    if got.strip() == text.strip():
        return True
    if allow_suffix and got.strip().endswith(text.strip()):
        return True
    print("!! paste verify failed: input readback mismatch (got %r)" % got[:60])
    return False

_last_send_ts = [0.0]
MIN_SEND_GAP_S = 3.0  # 任意两次发送之间的全局最小间隔，防机关枪连发被风控

def click(x, y):
    u.SetCursorPos(int(x), int(y)); time.sleep(0.12)
    u.mouse_event(2, 0, 0, 0, 0); time.sleep(0.06); u.mouse_event(4, 0, 0, 0, 0)

# ---------------------------------------------------------------- window
def find_wechat():
    """Find the REAL WeChat main window (Qt51514QWindowIcon), not the Chrome
    搜一搜 webview which also has title 微信.
    若主窗口被最小化到托盘（不可见），自动 ShowWindow 恢复。"""
    found = []
    hidden = []
    def cb(hwnd, lparam):
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value == "Qt51514QWindowIcon":
            length = u.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.startswith("微信"):
                if u.IsWindowVisible(hwnd):
                    found.append(hwnd)
                else:
                    hidden.append(hwnd)
        return True
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u.EnumWindows(WNDENUMPROC(cb), 0)
    # 微信可能同时有可见插件壳窗口和隐藏的真正主窗口。先在全部候选中寻找
    # session_list；命中隐藏主窗口时恢复它，不能只看可见候选。
    for hwnd in found + hidden:
        try:
            if find_session_list(hwnd) is not None:
                if hwnd in hidden:
                    u.ShowWindow(hwnd, 9)
                    time.sleep(0.8)
                return hwnd
        except Exception:
            continue
    if found:
        return found[0]
    if hidden:
        u.ShowWindow(hidden[0], 9)
        time.sleep(0.8)
        if u.IsWindowVisible(hidden[0]):
            return hidden[0]
    if not found:
        raise RuntimeError("WeChat main window not found (is 微信 running?)")
    raise RuntimeError("WeChat main window is not visible")

def force_foreground(hwnd):
    fg = u.GetForegroundWindow()
    tid_fg = u.GetWindowThreadProcessId(fg, None)
    tid_me = k32.GetCurrentThreadId()
    u.keybd_event(0x12, 0, 0, 0)
    u.AttachThreadInput(tid_me, tid_fg, True)
    u.ShowWindow(hwnd, 9)
    u.SetForegroundWindow(hwnd); u.BringWindowToTop(hwnd); u.SetActiveWindow(hwnd)
    u.AttachThreadInput(tid_me, tid_fg, False)
    u.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.4)
    return u.GetForegroundWindow() == hwnd

def _root(hwnd):
    from wxauto4.uia import uiautomation as uia
    return uia.ControlFromHandle(hwnd)

def _walk(c, d, fn, maxd=24):
    if d > maxd: return None
    try:
        got = fn(c)
    except Exception:
        return None
    if got is not None: return got
    try:
        ch = c.GetChildren()
    except Exception:
        return None
    for x in ch:
        got = _walk(x, d + 1, fn, maxd)
        if got is not None: return got
    return None

def _walk_all(c, d, fn, maxd=24):
    if d > maxd: return
    try:
        fn(c)
    except Exception:
        return
    try:
        ch = c.GetChildren()
    except Exception:
        return
    for x in ch:
        _walk_all(x, d + 1, fn, maxd)

# ---------------------------------------------------------------- locate
def find_search_edit(hwnd):
    return _walk(_root(hwnd), 0, lambda c: c if (c.ControlTypeName == "EditControl" and c.Name == "搜索") else None)

def find_search_result(hwnd, name):
    """在搜索结果里找真正的联系人/群聊项。
    只在「联系人」「群聊」分组内匹配；绝不点「搜一搜」「搜索网络结果」「聊天记录」。"""
    root = _root(hwnd)
    items = []
    def ci(x):
        if getattr(x, "ControlTypeName", "") == "ListItemControl":
            items.append(x)
    _walk_all(root, 0, ci, maxd=14)
    GOOD_SECTIONS = ("联系人", "群聊", "最常使用")
    BAD_HEADERS = ("聊天记录", "搜索网络结果", "公众号", "小程序", "表情", "朋友圈")
    section = None
    for it in items:
        nm = (it.Name or "").strip()
        if not nm:
            continue
        if nm in GOOD_SECTIONS:
            section = nm
            continue
        if nm in BAD_HEADERS:
            section = "BAD"
            continue
        if nm.startswith("查看全部") or nm.startswith("查看更多"):
            continue
        if "搜一搜" in nm:
            continue
        if section not in GOOD_SECTIONS:
            continue
        first = nm.split("\n")[0].strip()
        if first == name or first.startswith(name):
            return it
    return None

def find_chat_page(hwnd):
    c = _walk(_root(hwnd), 0, lambda c: c if c.AutomationId == "chat_message_page" else None)
    if c is None: return None
    r = c.BoundingRectangle
    return (r.left, r.top, r.right, r.bottom)

def find_session_list(hwnd):
    return _walk(_root(hwnd), 0, lambda c: c if c.AutomationId == "session_list" else None)

def find_message_list(hwnd):
    return _walk(_root(hwnd), 0, lambda c: c if c.AutomationId == "chat_message_list" else None)

# ---------------------------------------------------------------- read
def list_sessions(hwnd=None, retries=3):
    """Return [{name, last, raw}] for each conversation in the left sidebar.
    UIA 控件树可能在微信切换页面时短暂重建，先短重试，持续缺失才报错给自愈层。"""
    hwnd = hwnd or find_wechat()
    sl = None
    for _ in range(max(1, retries)):
        sl = find_session_list(hwnd)
        if sl is not None:
            break
        time.sleep(0.45)
    if sl is None:
        raise RuntimeError("session_list control not found")
    items = []
    def ci(x):
        if getattr(x, "ControlTypeName", "") == "ListItemControl":
            items.append(x)
    _walk_all(sl, 0, ci, maxd=10)
    out = []
    for it in items:
        raw = it.Name or ""
        lines = [l for l in raw.split("\n") if l.strip()]
        out.append({
            "name": lines[0] if lines else "",
            "last": lines[1] if len(lines) > 1 else "",
            "raw": raw,
        })
    return out

def read_chat(hwnd=None, limit=30, detect_side=True):
    """Read visible message bubbles from the currently open chat.
    Returns list of {kind, text, rect, side}.
    side: 'own' | 'other' | 'unknown' (via screenshot pixel analysis).
    Text bubbles: Name == text. Time rows appear as small ListItems like '12:52'."""
    hwnd = hwnd or find_wechat()
    ml = find_message_list(hwnd)
    if ml is None:
        return []
    items = []
    def ci(x):
        if getattr(x, "ControlTypeName", "") == "ListItemControl":
            items.append(x)
    _walk_all(ml, 0, ci, maxd=10)
    out = []
    for it in items[-limit:]:
        name = it.Name or ""
        r = it.BoundingRectangle
        kind = "text"
        if "\n" in name and ("文件" in name or ".pdf" in name or ".doc" in name or "微信电脑版" in name):
            kind = "file"
        elif name.replace(":", "").isdigit() and len(name) <= 5:
            kind = "time"
        elif "[图片]" in name or name.strip() == "图片":
            kind = "image"
        elif "[聊天记录]" in name:
            kind = "history"
        out.append({
            "kind": kind,
            "text": name,
            "rect": (r.left, r.top, r.right, r.bottom),
            "side": "unknown",
        })
    if detect_side and out:
        _annotate_sides(ml, out)
    return out


def _annotate_sides(ml, out):
    """Annotate own/other via screenshot: own bubble is green (#3x) on the
    right, other's bubble is dark grey on the left (deep theme).
    Fallback: bubble center x position."""
    try:
        from PIL import ImageGrab
    except Exception:
        return
    lr = ml.BoundingRectangle
    try:
        img = ImageGrab.grab(bbox=(lr.left, lr.top, lr.right, lr.bottom)).convert("RGB")
    except Exception:
        return
    W, H = img.size
    if W <= 0 or H <= 0:
        return
    # 背景色动态采样：取几个边缘点里出现最多的颜色当 BG，深/浅主题自适应
    from collections import Counter
    sample_pts = [(4, 4), (W - 5, 4), (4, H - 5), (W - 5, H - 5), (W // 2, 4), (W // 2, H - 5)]
    bg_votes = Counter(img.getpixel(p) for p in sample_pts if 0 <= p[0] < W and 0 <= p[1] < H)
    BG = bg_votes.most_common(1)[0][0] if bg_votes else (30, 30, 31)
    def close(c1, c2, tol=12):
        return all(abs(a - b) <= tol for a, b in zip(c1, c2))
    def classify(rect):
        ry = (rect[1] + rect[3]) // 2 - lr.top
        if ry < 0 or ry >= H:
            return "unknown"
        step = 4
        runs = []
        in_run = False
        start = 0
        acc = [0, 0, 0]
        n = 0
        def flush(x):
            nonlocal in_run, acc, n
            if in_run:
                runs.append((start, x, tuple(v // max(n, 1) for v in acc)))
                in_run = False
                acc = [0, 0, 0]
                n = 0
        for x in range(0, W, step):
            c = img.getpixel((x, ry))
            if not close(c, BG):
                if not in_run:
                    in_run = True
                    start = x
                acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]
                n += 1
            else:
                flush(x)
        flush(W)
        if not runs:
            return "unknown"
        main = max(runs, key=lambda s: s[1] - s[0])
        a, b, c = main
        if b - a < 10:
            return "unknown"
        # green bubble => own (WeChat deep theme own-bubble color)
        if c[1] > c[0] + 25 and c[1] > c[2] + 25:
            return "own"
        cx = (a + b) / 2
        if cx < W * 0.45:
            return "other"
        if cx > W * 0.55:
            return "own"
        return "unknown"
    for m in out:
        m["side"] = classify(m["rect"])
        if m["kind"] == "time":
            m["side"] = "unknown"

def current_chat_name(hwnd=None):
    """Read the big title label of the open chat (contact or group name)."""
    hwnd = hwnd or find_wechat()
    c = _walk(_root(hwnd), 0, lambda c: c if c.AutomationId == "content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.big_title_line_h_view.current_chat_name_label" else None)
    if c is None: return None
    return c.Name or None

def restart_wechat(exe_path=r"E:\Weixin\Weixin.exe", wait_s=45, launch_attempts=3):
    """重启微信并等待 UIA 主界面就绪。

    只在开始时终止一次旧进程。启动失败时重试拉起，但不会再次杀掉正在初始化的
    新进程，避免形成“刚启动又被杀”的循环。
    """
    import subprocess
    if not os.path.isfile(exe_path):
        raise RuntimeError(f"WeChat executable not found: {exe_path}")
    for pid_name in ("Weixin",):
        try:
            subprocess.run(["taskkill", "/F", "/IM", f"{pid_name}.exe"],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    time.sleep(3)
    proc = None
    launch_error = None
    for attempt in range(1, launch_attempts + 1):
        try:
            proc = subprocess.Popen([exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            if proc.poll() is None:
                break
            launch_error = RuntimeError(f"WeChat exited immediately with code {proc.returncode}")
        except Exception as e:
            launch_error = e
        if attempt < launch_attempts:
            time.sleep(2)
    if proc is None or proc.poll() is not None:
        raise RuntimeError(f"cannot launch WeChat: {launch_error}")

    # 登录确认界面：周期性前置 + Enter，直到 session_list 控件出现。
    t0 = time.time()
    last_enter = 0.0
    last_error = None
    while time.time() - t0 < wait_s:
        try:
            hwnd = find_wechat()
            force_foreground(hwnd)
            if find_session_list(hwnd) is not None:
                return hwnd
            if time.time() - last_enter >= 5:
                key(0x0D)
                last_enter = time.time()
        except Exception as e:
            last_error = e
        if proc.poll() is not None:
            raise RuntimeError(f"WeChat exited during startup with code {proc.returncode}")
        time.sleep(2)
    raise RuntimeError(f"restart_wechat: UIA main window not ready after {wait_s}s; last_error={last_error}")

# ---------------------------------------------------------------- @ member
def find_mention_list(hwnd):
    return _walk(_root(hwnd), 0, lambda c: c if c.AutomationId == "chat_mention_list" else None)

def at_member(hwnd, name, timeout=5.0):
    """在当前打开的群聊里 @ 指定成员（输入框需已聚焦）。
    流程：输 @ → 等 chat_mention_list 面板 → 输名字过滤 → 点匹配成员项。
    成功后输入框里会有 @名字 标签，光标在其后，可继续输入正文。"""
    type_unicode("@", delay=0.06)
    t0 = time.time()
    ml = None
    while time.time() - t0 < timeout:
        ml = find_mention_list(hwnd)
        if ml is not None:
            break
        time.sleep(0.3)
    if ml is None:
        print("!! mention list did not appear")
        return False
    filt = name[:6]
    if filt:
        type_unicode(filt, delay=0.06)
        time.sleep(0.9)
    t0 = time.time()
    while time.time() - t0 < timeout:
        ml = find_mention_list(hwnd)
        if ml is None:
            return False
        items = []
        def ci(x):
            if getattr(x, "ControlTypeName", "") == "ListItemControl":
                items.append(x)
        _walk_all(ml, 0, ci, maxd=6)
        target = None
        for it in items:
            nm = (it.Name or "").strip()
            if nm == name or nm.startswith(name) or (name and name.startswith(nm)):
                target = it
                break
        if target is None and items:
            target = items[0]
        if target is not None:
            r = target.BoundingRectangle
            click((r.left + r.right) / 2, (r.top + r.bottom) / 2)
            time.sleep(0.6)
            return True
        time.sleep(0.4)
    return False

# ---------------------------------------------------------------- send
def open_chat_by_click(hwnd, name, timeout=6.0):
    """直接在会话列表里点击打开会话（不经过搜索框）。"""
    force_foreground(hwnd)
    sl = find_session_list(hwnd)
    if sl is None:
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        items = []
        def ci(x):
            if getattr(x, "ControlTypeName", "") == "ListItemControl":
                items.append(x)
        _walk_all(sl, 0, ci, maxd=10)
        for it in items:
            nm = it.Name or ""
            if nm.split("\n")[0] == name:
                r = it.BoundingRectangle
                click((r.left + r.right) / 2, (r.top + r.bottom) / 2)
                time.sleep(1.5)
                return True
        time.sleep(0.5)
    return False

def open_chat(hwnd, contact, timeout=6.0):
    """搜索框输入对象，点击搜出来的对象（绝不点搜一搜）。Returns chat page rect."""
    force_foreground(hwnd)
    se = find_search_edit(hwnd)
    if se is None: raise RuntimeError("search edit not found")
    se.SetFocus(); time.sleep(0.3)
    se.GetValuePattern().SetValue(contact)
    t0 = time.time()
    item = None
    while time.time() - t0 < timeout:
        item = find_search_result(hwnd, contact)
        if item is not None: break
        time.sleep(0.4)
    if item is None: raise RuntimeError(f"search result for {contact!r} not found")
    r = item.BoundingRectangle
    click((r.left + r.right) / 2, (r.top + r.bottom) / 2)
    time.sleep(1.0)
    cp = find_chat_page(hwnd)
    if cp is None: raise RuntimeError("chat page not found after opening chat")
    return cp

def send_text(contact, text):
    """打开会话（优先会话列表点击，找不到才搜索）并发送文字。Returns True.
    发送方式：真实剪贴板 + Ctrl+V 粘贴，粘贴后强制回读验证；
    验证失败重试一次，仍失败才回退逐字打字并大声告警。"""
    gap = time.time() - _last_send_ts[0]
    if gap < MIN_SEND_GAP_S:
        time.sleep(MIN_SEND_GAP_S - gap)
    hwnd = find_wechat()
    cp = find_chat_page(hwnd)
    cur = current_chat_name(hwnd) if cp is not None else None
    if cp is None or cur != contact:
        ok = open_chat_by_click(hwnd, contact)
        if ok:
            cp = find_chat_page(hwnd)
        else:
            cp = open_chat(hwnd, contact)
    if cp is None:
        raise RuntimeError(f"cannot open chat with {contact!r}")
    force_foreground(hwnd)
    # 点击输入框，坐标带随机抖动，更像真人
    ix = (cp[0] + cp[2]) / 2 + random.randint(-14, 14)
    iy = cp[3] - 90 + random.randint(-6, 6)
    click(ix, iy)
    time.sleep(random.uniform(0.35, 0.6))
    ok = False
    try:
        ok = paste_verified(text)
    except Exception as e:
        print("paste error:", e)
    if not ok:
        # 重试一次：重新点输入框再粘
        try:
            click(ix, iy)
            time.sleep(0.4)
            ok = paste_verified(text)
        except Exception as e:
            print("paste retry error:", e)
    if not ok:
        print("!! PASTE FAILED TWICE — fallback to typing (injection risk, investigate!)")
        type_unicode(text)
        time.sleep(0.4)
    time.sleep(random.uniform(0.6, 1.2))  # 粘贴后到回车前的人性化停顿
    key(0x0D)  # Enter sends in WeChat PC default
    time.sleep(0.8)
    _last_send_ts[0] = time.time()
    return True

def send_text_at(contact, at_name, text):
    """群聊里先 @ 成员再发正文。at_name 为空则退化为普通 send_text。"""
    if not at_name:
        return send_text(contact, text)
    gap = time.time() - _last_send_ts[0]
    if gap < MIN_SEND_GAP_S:
        time.sleep(MIN_SEND_GAP_S - gap)
    hwnd = find_wechat()
    cp = find_chat_page(hwnd)
    cur = current_chat_name(hwnd) if cp is not None else None
    if cp is None or cur != contact:
        ok = open_chat_by_click(hwnd, contact)
        if ok:
            cp = find_chat_page(hwnd)
        else:
            cp = open_chat(hwnd, contact)
    if cp is None:
        raise RuntimeError(f"cannot open chat with {contact!r}")
    force_foreground(hwnd)
    ix = (cp[0] + cp[2]) / 2 + random.randint(-14, 14)
    iy = cp[3] - 90 + random.randint(-6, 6)
    click(ix, iy)
    time.sleep(random.uniform(0.35, 0.6))
    ok_at = False
    try:
        ok_at = at_member(hwnd, at_name)
    except Exception as e:
        print("at_member error:", e)
    if not ok_at:
        # @ 失败：清掉输入框残留的 @过滤字，退化为正文前加文字 @
        select_all(); key(0x2E)
        time.sleep(0.2)
        click(ix, iy)
        time.sleep(0.3)
        text = f"@{at_name} " + text
    # 粘贴正文（此时输入框可能已有 @ 标签，光标在其后；读回含 @标签故用 suffix 验证）
    ok = False
    try:
        ok = paste_verified(text, allow_suffix=ok_at)
    except Exception as e:
        print("paste error:", e)
    if not ok:
        try:
            click(ix, iy)
            time.sleep(0.4)
            ok = paste_verified(text, allow_suffix=ok_at)
        except Exception as e:
            print("paste retry error:", e)
    if not ok:
        print("!! PASTE FAILED — fallback typing")
        type_unicode(text)
        time.sleep(0.4)
    time.sleep(random.uniform(0.6, 1.2))
    key(0x0D)
    time.sleep(0.8)
    _last_send_ts[0] = time.time()
    return True

# ---------------------------------------------------------------- image send
CF_DIB = 8

def set_clipboard_image(path):
    """把图片文件放到剪贴板（CF_DIB 位图格式），粘贴到微信会以图片消息发送。"""
    import struct
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    row_size = (w * 3 + 3) & ~3
    px = im.load()
    buf = bytearray()
    # DIB: bottom-up, BGR
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b = px[x, y]
            row += bytes((b, g, r))
        row += b"\x00" * (row_size - w * 3)
        buf += row
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(buf), 0, 0, 0, 0)
    data = header + bytes(buf)
    for _ in range(5):
        if u.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("OpenClipboard failed")
    try:
        u.EmptyClipboard()
        hg = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hg:
            raise RuntimeError("GlobalAlloc failed")
        ptr = k32.GlobalLock(hg)
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(hg)
        if not u.SetClipboardData(CF_DIB, hg):
            raise RuntimeError("SetClipboardData failed")
    finally:
        u.CloseClipboard()

def send_image(contact, path):
    """打开会话并以图片形式发送本地图片。"""
    gap = time.time() - _last_send_ts[0]
    if gap < MIN_SEND_GAP_S:
        time.sleep(MIN_SEND_GAP_S - gap)
    hwnd = find_wechat()
    cp = find_chat_page(hwnd)
    cur = current_chat_name(hwnd) if cp is not None else None
    if cp is None or cur != contact:
        ok = open_chat_by_click(hwnd, contact)
        if ok:
            cp = find_chat_page(hwnd)
        else:
            cp = open_chat(hwnd, contact)
    if cp is None:
        raise RuntimeError(f"cannot open chat with {contact!r}")
    force_foreground(hwnd)
    ix = (cp[0] + cp[2]) / 2 + random.randint(-14, 14)
    iy = cp[3] - 90 + random.randint(-6, 6)
    click(ix, iy)
    time.sleep(random.uniform(0.4, 0.7))
    set_clipboard_image(path)
    paste()
    time.sleep(random.uniform(1.2, 2.0))  # 图片粘贴后等预览加载
    key(0x0D)
    time.sleep(1.0)
    _last_send_ts[0] = time.time()
    return True

# ---------------------------------------------------------------- emoji
_EMOJI_BTN_DX = 60   # 表情按钮相对 chat_page 左下角的偏移
_EMOJI_BTN_DY = 59

def _enum_qt_popups():
    """枚举所有 Qt popup/tool 顶层窗口句柄。"""
    hits = []
    def cb(h, lp):
        if u.IsWindowVisible(h):
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(h, cls, 256)
            if cls.value.startswith("Qt51514QWindowTool") or cls.value.startswith("Qt51514QWindowPopup"):
                hits.append(h)
        return True
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    u.EnumWindows(WNDENUMPROC(cb), 0)
    return hits

def wait_emoticon_popover(timeout=4.0):
    """等表情面板（aid=EmoticonPopover）出现，返回其 root Control。"""
    from wxauto4.uia import uiautomation as uia
    t0 = time.time()
    while time.time() - t0 < timeout:
        for h in _enum_qt_popups():
            try:
                root = uia.ControlFromHandle(h)
                if root.AutomationId == "EmoticonPopover":
                    return root
                c = _walk(root, 0, lambda x: x if x.AutomationId == "EmoticonPopover" else None, maxd=3)
                if c is not None:
                    return c
            except Exception:
                continue
        time.sleep(0.3)
    return None

def _emoji_item(pop, name):
    """在表情面板里按名字找表情项（TextControl），返回其可点击 ButtonControl 的中心坐标。"""
    found = []
    def fn(c):
        try:
            if c.ControlTypeName == "TextControl" and (c.Name or "").strip() == name:
                r = c.BoundingRectangle
                # 点 TextControl 中心即可（ButtonControl 在其内部）
                found.append(((r.left + r.right) / 2, (r.top + r.bottom) / 2))
        except Exception:
            pass
        return None
    _walk(pop, 0, fn, maxd=14)
    return found[0] if found else None

def _emoji_tab_pos(pop, tab_name):
    """在表情面板里找指定 tab（TabItemControl）的中心坐标。"""
    pos = []
    def ft(c):
        try:
            if c.ControlTypeName == "TabItemControl" and (c.Name or "").strip() == tab_name:
                r = c.BoundingRectangle
                pos.append(((r.left + r.right) / 2, (r.top + r.bottom) / 2))
        except Exception:
            pass
        return None
    _walk(pop, 0, ft, maxd=14)
    return pos[0] if pos else None


def list_custom_sticker_buttons(pop):
    """列出自定义表情 tab 里的贴纸格子（ButtonControl，行优先排序），返回 rect 列表。"""
    btns = []
    def fb(c):
        try:
            if c.ControlTypeName == "ButtonControl":
                r = c.BoundingRectangle
                if r.right - r.left > 50 and r.bottom - r.top > 50:
                    btns.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        except Exception:
            pass
        return None
    _walk(pop, 0, fb, maxd=16)
    return sorted(set(btns), key=lambda b: (b[1], b[0]))


_CUSTOM_TAB = "自定义表情"


def right_click(x, y):
    """鼠标右键点击（物理坐标）。"""
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.15)
    u.mouse_event(0x0008, 0, 0, 0, 0)  # RIGHTDOWN
    time.sleep(0.05)
    u.mouse_event(0x0010, 0, 0, 0, 0)  # RIGHTUP


def click_context_menu(item_name, timeout=3.0):
    """在右键弹出的上下文菜单里点指定 MenuItemControl（如 '引用'/'复制'）。"""
    from wxauto4.uia import uiautomation as uia
    t0 = time.time()
    while time.time() - t0 < timeout:
        for h in _enum_qt_popups():
            try:
                root = uia.ControlFromHandle(h)
                pos = [None]
                def fn(c):
                    try:
                        if c.ControlTypeName == "MenuItemControl" and (c.Name or "").strip() == item_name:
                            r = c.BoundingRectangle
                            pos[0] = ((r.left + r.right) / 2, (r.top + r.bottom) / 2)
                    except Exception:
                        pass
                    return None
                _walk(root, 0, fn, maxd=8)
                if pos[0]:
                    click(pos[0][0], pos[0][1])
                    return True
            except Exception:
                continue
        time.sleep(0.3)
    return False


def quote_reply(contact, bubble_rect, text):
    """引用指定气泡的消息并回复。bubble_rect = read_chat 里那条消息的 rect。
    流程：右键气泡中心 → 菜单点「引用」→ 粘贴文本 → 回车。"""
    gap = time.time() - _last_send_ts[0]
    if gap < MIN_SEND_GAP_S:
        time.sleep(MIN_SEND_GAP_S - gap)
    hwnd = find_wechat()
    cp = find_chat_page(hwnd)
    cur = current_chat_name(hwnd) if cp is not None else None
    if cp is None or cur != contact:
        ok = open_chat_by_click(hwnd, contact)
        if ok:
            cp = find_chat_page(hwnd)
        else:
            cp = open_chat(hwnd, contact)
    if cp is None:
        raise RuntimeError(f"cannot open chat with {contact!r}")
    force_foreground(hwnd)
    l, t, r, b = [int(v) for v in bubble_rect]
    right_click((l + r) / 2, (t + b) / 2)
    time.sleep(0.8)
    if not click_context_menu("引用"):
        print("!! 引用 menu item not found, fallback to plain send")
        key(0x1B)
        time.sleep(0.3)
        return send_text(contact, text)
    time.sleep(0.8)
    paste_verified(text, allow_suffix=True)
    time.sleep(0.4)
    key(0x0D)
    time.sleep(0.6)
    _last_send_ts[0] = time.time()
    return True


def set_clipboard_files(paths):
    """把文件路径列表放上剪贴板（CF_HDROP），粘贴后作为文件消息。"""
    import struct
    CF_HDROP = 15
    GHND = 0x0042
    files_w = "\x00".join(paths) + "\x00\x00"
    data = files_w.encode("utf-16-le")
    drop = struct.pack("<IiiII", 20, 0, 0, 0, 1) + data
    for _ in range(10):
        if u.OpenClipboard(None):
            break
        time.sleep(0.1)
    u.EmptyClipboard()
    h = k32.GlobalAlloc(GHND, len(drop))
    ptr = k32.GlobalLock(h)
    ctypes.memmove(ptr, drop, len(drop))
    k32.GlobalUnlock(h)
    u.SetClipboardData(CF_HDROP, h)
    u.CloseClipboard()


def send_file(contact, path):
    """以文件消息形式发送本地文件（CF_HDROP 剪贴板方案）。"""
    gap = time.time() - _last_send_ts[0]
    if gap < MIN_SEND_GAP_S:
        time.sleep(MIN_SEND_GAP_S - gap)
    hwnd = find_wechat()
    cp = find_chat_page(hwnd)
    cur = current_chat_name(hwnd) if cp is not None else None
    if cp is None or cur != contact:
        ok = open_chat_by_click(hwnd, contact)
        if ok:
            cp = find_chat_page(hwnd)
        else:
            cp = open_chat(hwnd, contact)
    if cp is None:
        raise RuntimeError(f"cannot open chat with {contact!r}")
    force_foreground(hwnd)
    set_clipboard_files([os.path.abspath(path)])
    click(cp[0] + 400, cp[3] - 80)
    time.sleep(0.3)
    paste()
    time.sleep(1.5)
    key(0x0D)
    time.sleep(1.5)
    _last_send_ts[0] = time.time()
    return True


def send_sticker(contact, index):
    """发送自定义表情（爱心收藏 tab）里第 index 张贴纸（1 起，行优先从左到右）。
    贴纸点击即发送，无需回车。index 超出范围返回 False。"""
    gap = time.time() - _last_send_ts[0]
    if gap < MIN_SEND_GAP_S:
        time.sleep(MIN_SEND_GAP_S - gap)
    hwnd = find_wechat()
    cp = find_chat_page(hwnd)
    cur = current_chat_name(hwnd) if cp is not None else None
    if cp is None or cur != contact:
        ok = open_chat_by_click(hwnd, contact)
        if ok:
            cp = find_chat_page(hwnd)
        else:
            cp = open_chat(hwnd, contact)
    if cp is None:
        raise RuntimeError(f"cannot open chat with {contact!r}")
    force_foreground(hwnd)
    click(cp[0] + _EMOJI_BTN_DX + random.randint(-4, 4), cp[3] - _EMOJI_BTN_DY + random.randint(-3, 3))
    pop = wait_emoticon_popover(timeout=4.0)
    if pop is None:
        print("!! emoticon popover not found")
        return False
    time.sleep(0.5)
    tp = _emoji_tab_pos(pop, _CUSTOM_TAB)
    if tp is None:
        print(f"!! tab {_CUSTOM_TAB!r} not found")
        key(0x1B)
        return False
    click(tp[0], tp[1])
    time.sleep(1.0)
    btns = list_custom_sticker_buttons(pop)
    if not (1 <= index <= len(btns)):
        print(f"!! sticker index {index} out of range (1..{len(btns)})")
        key(0x1B)
        return False
    l, t, r, b = btns[index - 1]
    click((l + r) / 2 + random.randint(-3, 3), (t + b) / 2 + random.randint(-3, 3))
    time.sleep(0.8)
    key(0x1B)  # 贴纸点击即发，ESC 收尾关面板（若还开着）
    _last_send_ts[0] = time.time()
    return True


def send_emoji(contact, emoji_name, tab_name=None):
    """发送微信表情。emoji_name 如 '微笑'/'旺柴'/'捂脸'。
    tab_name=None 用当前 tab（默认表情）；指定如 '自定义表情'/'赞萌露比' 则先切 tab。
    小黄脸进输入框后回车发送；贴纸类点击直接发送。"""
    gap = time.time() - _last_send_ts[0]
    if gap < MIN_SEND_GAP_S:
        time.sleep(MIN_SEND_GAP_S - gap)
    hwnd = find_wechat()
    cp = find_chat_page(hwnd)
    cur = current_chat_name(hwnd) if cp is not None else None
    if cp is None or cur != contact:
        ok = open_chat_by_click(hwnd, contact)
        if ok:
            cp = find_chat_page(hwnd)
        else:
            cp = open_chat(hwnd, contact)
    if cp is None:
        raise RuntimeError(f"cannot open chat with {contact!r}")
    force_foreground(hwnd)
    click(cp[0] + _EMOJI_BTN_DX + random.randint(-4, 4), cp[3] - _EMOJI_BTN_DY + random.randint(-3, 3))
    pop = wait_emoticon_popover(timeout=4.0)
    if pop is None:
        print("!! emoticon popover not found")
        return False
    time.sleep(0.5)
    if tab_name:
        # 切 tab
        tab_pos = None
        def ft(c):
            nonlocal tab_pos
            try:
                if c.ControlTypeName == "TabItemControl" and (c.Name or "").strip() == tab_name:
                    r = c.BoundingRectangle
                    tab_pos = ((r.left + r.right) / 2, (r.top + r.bottom) / 2)
            except Exception:
                pass
            return None
        _walk(pop, 0, ft, maxd=14)
        if tab_pos:
            click(tab_pos[0], tab_pos[1])
            time.sleep(0.8)
        else:
            print(f"!! emoji tab {tab_name!r} not found")
    pos = _emoji_item(pop, emoji_name)
    if pos is None:
        print(f"!! emoji {emoji_name!r} not found in panel")
        key(0x1B)
        return False
    click(pos[0], pos[1])
    time.sleep(0.7)
    # 小黄脸会插入输入框，回车发送；贴纸点击即发出（回车无妨，输入框为空时 Enter 不发送）
    key(0x0D)
    time.sleep(0.6)
    # 若面板还开着就关掉
    key(0x1B)
    _last_send_ts[0] = time.time()
    return True

if __name__ == "__main__":
    import json
    hwnd = find_wechat()
    print("== sessions ==")
    for s in list_sessions(hwnd):
        print(json.dumps(s, ensure_ascii=False))
    print("== current chat ==")
    print("name:", current_chat_name(hwnd))
    for m in read_chat(hwnd, limit=15):
        print(json.dumps(m, ensure_ascii=False))
