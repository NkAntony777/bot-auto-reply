# -*- coding: utf-8 -*-
"""wxbot 安全停止：优雅退出优先，强杀兜底。

流程：扫描守护进程 → 写停止标记（wxbot.stop）→ 等待主循环检测到标记后
走 finally 清理（保存状态、清 pid 文件、把微信从虚拟屏还回主屏）→
超时未退则询问是否强制 → 停看板（无状态观察者，直接结束）→
微信位置兜底检查（强杀路径下守护进程没机会还屏，这里补上）。

用法：python stop_wxbot.py            # 正常停止
      python stop_wxbot.py --status   # 只查看状态，不做任何操作
"""
import ctypes
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import wxbot  # 复用 _wxbot_script_pids / STOP_FILE
import wxmini2 as wx

GRACE_TIMEOUT_S = 20


def pid_alive(pid):
    k = ctypes.windll.kernel32
    SYNCHRONIZE = 0x00100000
    h = k.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    k.CloseHandle(h)
    return True


def wechat_parked_on_secondary():
    """返回 (是否停靠在虚拟屏, 微信 hwnd)。"""
    h = wx.find_wechat()
    if not h:
        return False, None
    sec = wx.secondary_screen_rect()
    if not sec:
        return False, h
    l, t, r, b, w, hh = wx.get_window_rect(h)
    return (l >= sec[0] - 8), h


def stop_dashboard(dash_pids):
    for p in dash_pids:
        subprocess.run(["taskkill", "/F", "/PID", str(p)], capture_output=True)


def main():
    status_only = "--status" in sys.argv
    daemon_pids = wxbot._wxbot_script_pids("wxbot.py")
    dash_pids = wxbot._wxbot_script_pids("wxbot_dashboard.py")
    parked, _hwnd = wechat_parked_on_secondary()
    print(f"[stop] 守护进程: {daemon_pids or '未运行'} | 看板: {dash_pids or '未运行'} | "
          f"微信: {'停靠在虚拟屏' if parked else '主屏/无副屏'}")
    if status_only:
        return

    # 1) 优雅停止守护进程
    if daemon_pids:
        with open(wxbot.STOP_FILE, "w", encoding="utf-8") as f:
            f.write("stop\n")
        print(f"[stop] 已写停止标记，等待守护进程优雅退出（最长 {GRACE_TIMEOUT_S}s）...")
        t0 = time.time()
        while time.time() - t0 < GRACE_TIMEOUT_S:
            if not any(pid_alive(p) for p in daemon_pids):
                break
            time.sleep(1)
        if any(pid_alive(p) for p in daemon_pids):
            ans = input(f"[stop] {GRACE_TIMEOUT_S}s 未退出（可能在回复发送中途）。强制结束？(Y/N) ")
            if ans.strip().lower() in ("y", ""):
                for p in daemon_pids:
                    subprocess.run(["taskkill", "/F", "/PID", str(p)], capture_output=True)
                print("[stop] 已强制结束（状态每轮都有保存，损失可控）")
            else:
                print("[stop] 保留运行：守护进程会在下一轮读到停止标记后自行退出，"
                      "看板暂不停。下次运行本脚本可再试。")
                return
        else:
            print("[stop] 守护进程已优雅退出（状态已保存，微信已还回主屏）")
    else:
        print("[stop] 守护进程未在运行")

    # 2) 清停止标记（防遗留影响下次启动）
    try:
        if os.path.exists(wxbot.STOP_FILE):
            os.remove(wxbot.STOP_FILE)
    except OSError:
        pass

    # 3) 停看板（无状态观察者，直接结束即可）
    if dash_pids:
        stop_dashboard(dash_pids)
        print(f"[stop] 看板已停止 {dash_pids}")

    # 4) 微信位置兜底：强杀路径下守护进程没机会走 finally，这里补还屏
    parked, hwnd = wechat_parked_on_secondary()
    if parked and hwnd:
        wx.restore_wechat_to_primary(hwnd)
        print("[stop] 微信已从虚拟屏还回主屏（兜底恢复）")
    print("[stop] 完成。")


if __name__ == "__main__":
    main()
