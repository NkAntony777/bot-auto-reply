# -*- coding: utf-8 -*-
"""快筛 gate 测试：解析单测（离线）+ 真实 API 判定（可选 --live）。

用法：
    .venv/Scripts/python.exe -X utf8 tests/test_screener.py           # 离线解析单测
    .venv/Scripts/python.exe -X utf8 tests/test_screener.py --live    # 加真实 step-3.5-fast 判定
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wxbot_screener as ws


def test_parse():
    ok = ws.parse_verdict('{"reply": true, "reason": "被问了问题"}')
    assert ok == (True, "被问了问题"), ok
    ok = ws.parse_verdict('前置废话 {"reply": false, "reason": "纯表情"} 后置废话')
    assert ok == (False, "纯表情"), ok
    ok = ws.parse_verdict('{"reply": "yes", "reason": "x"}')
    assert ok[0] is True
    ok = ws.parse_verdict('{"reply": "no", "reason": "x"}')
    assert ok[0] is False
    assert ws.parse_verdict('{"foo": 1}') is None
    assert ws.parse_verdict("NO\n不需要回") == (False, "bare-token")
    assert ws.parse_verdict("YES") == (True, "bare-token")
    assert ws.parse_verdict("") is None
    print("parse tests OK")


def test_fail_open():
    """通道全挂时必须放行（fail-open）。"""
    orig = ws._post_json
    def boom(*a, **k):
        raise RuntimeError("network down")
    ws._post_json = boom
    try:
        with io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "wxbot_config.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        ok, why = ws.should_reply(cfg, "测试", "在吗？", ["对方: 在吗"], False)
        assert ok and why == "fail-open", (ok, why)
        print("fail-open OK")
    finally:
        ws._post_json = orig


def test_live():
    with io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "wxbot_config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    assert ws.active(cfg), "screener.enabled 应为 true"
    cases = [
        # (场景, incoming, ctx, is_group, 期望值得回)
        ("私聊被问问题", "你明天有空吗？想问你点事", ["对方: 在吗", "我: 在的"], False, True),
        ("私聊纯表情收尾", "[表情]哈哈哈", ["我: 那就这样定啦", "对方: 哈哈哈"], False, False),
        ("群聊被@", "【发送者昵称: 老王】【普通群友: 必须礼貌友善、积极帮助】@阿廖沙 你觉得呢", ["老王: 这事你们怎么看", "我: 我再想想"], True, True),
        ("群聊无关闲聊", "【发送者昵称: 老王】【普通群友: 必须礼貌友善、积极帮助】今天午饭吃了牛肉面", ["老王: 中午吃啥", "李四: 随便"], True, False),
    ]
    passed = 0
    for label, incoming, ctx, is_group, expect in cases:
        ok, why = ws.should_reply(cfg, label, incoming, ctx, is_group)
        mark = "✔" if ok == expect else "✘"
        print(f"  {mark} {label}: reply={ok} (期望 {expect}) reason={why}")
        passed += (ok == expect)
    print(f"live cases: {passed}/{len(cases)}")
    assert passed >= 3, "至少 3/4 判定正确"


if __name__ == "__main__":
    test_parse()
    test_fail_open()
    if "--live" in sys.argv:
        test_live()
    print("ALL PASSED ✔")
