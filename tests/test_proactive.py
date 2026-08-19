# -*- coding: utf-8 -*-
"""主动说话引擎离线测试：假 wx + 替身 _llm_call，验证各门控分支（不碰微信、不调真 API）。

用法：.venv/Scripts/python.exe -X utf8 tests/test_proactive.py
"""
import copy
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wxbot

TEST_CONV = "离线测试-主动说话"


class FakeWx:
    def __init__(self, msgs):
        self.msgs = msgs
        self.sent = []
    def db_sessions(self, limit=30):
        return [{"name": TEST_CONV, "username": "wxid_test_000", "last": ""}]
    def read_chat_db(self, username, limit=10):
        return self.msgs
    def send_text(self, name, text):
        self.sent.append((name, text))
        return True


def base_cfg():
    with io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "wxbot_config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg = copy.deepcopy(cfg)
    cfg["proactive"] = {
        "enabled": True,
        "conversations": [TEST_CONV],
        "min_silence_min": 0.02,     # ~1.2s，测试用
        "min_interval_min": 0.05,    # ~3s
        "max_per_day": 2,
        "active_hours": {},
    }
    return cfg


def fresh_state():
    return wxbot.State(os.path.join(tempfile.gettempdir(), f"wxbot_test_proactive_{int(time.time())}.json"))


def old_msgs(hours_ago=3):
    ts = time.time() - hours_ago * 3600
    return [
        {"side": "other", "kind": "text", "text": "上次说的那只猫后来怎么样了", "ts": ts - 60, "sender": ""},
        {"side": "own", "kind": "text", "text": "还在养着，胖了", "ts": ts},
    ]


def run_tick(cfg, state, fake, llm_reply):
    state.data.setdefault("proactive", {})["tick_ts"] = 0   # 绕过 60s 节流（各场景独立测）
    calls = []
    orig = wxbot._llm_call
    wxbot._llm_call = lambda c, s, u: (calls.append(u), llm_reply)[1]
    try:
        wxbot._proactive_tick(cfg, state, fake)
    finally:
        wxbot._llm_call = orig
    return calls


def main():
    # --- 时段判断 ---
    assert wxbot._in_active_hours({}) is True
    assert wxbot._in_active_hours({"start": "00:00", "end": "23:59"}) is True
    h = time.localtime().tm_hour
    if 1 <= h <= 22:
        assert wxbot._in_active_hours({"start": "00:00", "end": f"{h:02d}:59"}) is True
    assert wxbot._in_active_hours({"start": "23:00", "end": "06:00"}) == (h >= 23 or h < 6)
    print("active_hours OK")

    # --- 标记剥离 ---
    body = wxbot._strip_capability_markers("在吗？\n[IMG:猫]\n[EMOJI:捂脸]\n@张三 你好\n[AUDIO:x]\n[Q] 引用")
    assert body == "在吗？", body
    print("strip markers OK")

    # --- ctx 行构建 ---
    lines = wxbot._ctx_from_msgs(old_msgs(), 8, False)
    assert lines == ["对方: 上次说的那只猫后来怎么样了", "我: 还在养着，胖了"], lines
    print("ctx_from_msgs OK")

    # --- 场景1：静默够久 + LLM 说 SKIP → 不发送，但记账 ---
    cfg, state, fake = base_cfg(), fresh_state(), FakeWx(old_msgs())
    calls = run_tick(cfg, state, fake, "[SKIP]")
    assert calls and not fake.sent, (calls, fake.sent)
    assert TEST_CONV in state.data["proactive"], "应记录尝试时间"
    print("SKIP 场景 OK")

    # --- 场景2：间隔没到 → 不再调 LLM ---
    calls = run_tick(cfg, state, fake, "又来一句")
    assert not calls and not fake.sent, (calls, fake.sent)
    print("间隔门控 OK")

    # --- 场景3：间隔到了 + 对方在我们上次主动之后回过话 + LLM 给话 → 发送 + 日计数 ---
    state.data["proactive"][TEST_CONV] = {"ts": time.time() - 600}   # 10 分钟前主动过
    fake.msgs = [  # 对方 5 分钟前回过话（晚于上次主动），之后静默
        {"side": "other", "kind": "text", "text": "猫还好吧", "ts": time.time() - 300, "sender": ""},
        {"side": "own", "kind": "text", "text": "好着呢", "ts": time.time() - 240},
    ]
    calls = run_tick(cfg, state, fake, "突然想起你说的那只猫，最近还拆家吗")
    assert calls and len(fake.sent) == 1, (calls, fake.sent)
    assert state.data["proactive"]["count"] == 1
    print("发送场景 OK")

    # --- 场景4：日上限 → 直接不动作 ---
    state.data["proactive"][TEST_CONV] = {"ts": time.time() - 600}
    state.data["proactive"]["count"] = 2  # max_per_day=2
    calls = run_tick(cfg, state, fake, "还想说")
    assert not calls and len(fake.sent) == 1
    print("日上限 OK")

    # --- 场景5：上次主动没人回（other 最新消息早于上次主动）→ 不再推 ---
    state2, fake2 = fresh_state(), FakeWx(old_msgs(hours_ago=8))
    pdata = state2.data["proactive"]
    pdata[TEST_CONV] = {"ts": time.time() - 3600}   # 1 小时前主动过（>间隔）
    calls = run_tick(cfg, state2, fake2, "再推一句")
    assert not calls and not fake2.sent, (calls, fake2.sent)
    print("无人回应不追问 OK")

    # --- 场景6：对话还热着（刚聊过）→ 不开口 ---
    state3, fake3 = fresh_state(), FakeWx(old_msgs(hours_ago=0))  # 3 小时前=0？old_msgs(0)=现在
    hot = old_msgs(0.0001)  # 0.36s 前
    fake3.msgs = hot
    calls = run_tick(cfg, state3, fake3, "插嘴")
    assert not calls and not fake3.sent, (calls, fake3.sent)
    print("对话热时不插嘴 OK")

    # --- 场景7：时段限制（active_hours 设为不可能的区间）→ 不动作 ---
    cfg7 = base_cfg()
    cfg7["proactive"]["active_hours"] = {"start": "03:33", "end": "03:34"}
    if not (time.localtime().tm_hour == 3 and time.localtime().tm_min == 33):
        state7, fake7 = fresh_state(), FakeWx(old_msgs())
        calls = run_tick(cfg7, state7, fake7, "说")
        assert not calls and not fake7.sent
        print("时段限制 OK")

    # --- 场景8：disabled → 直接返回 ---
    cfg8 = base_cfg()
    cfg8["proactive"]["enabled"] = False
    state8, fake8 = fresh_state(), FakeWx(old_msgs())
    calls = run_tick(cfg8, state8, fake8, "说")
    assert not calls and not fake8.sent
    print("总开关 OK")

    print("ALL PASSED ✔")


if __name__ == "__main__":
    main()
