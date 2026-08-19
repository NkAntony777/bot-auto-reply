# -*- coding: utf-8 -*-
"""叫名字机制测试：_name_called 识别 + 配置完整性（离线）。

用法：.venv/Scripts/python.exe -X utf8 tests/test_name_call.py
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wxbot

CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wxbot_config.json")


def main():
    with io.open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # 配置完整性
    assert "阿廖沙" in cfg["reply"]["call_names"] and "沙沙" in cfg["reply"]["call_names"], cfg["reply"].get("call_names")
    assert float(cfg["reply"].get("settle_s_called", 0)) > 0
    print("config OK:", cfg["reply"]["call_names"], "settle_s_called =", cfg["reply"]["settle_s_called"])

    # 名字识别（call_names + own_nicknames + mention_names 任一子串命中）
    cases = [
        ("阿廖沙你觉得呢", True),
        ("沙沙在吗", True),
        ("@阿廖沙 出来", True),                      # 文字 @ 也含名字
        ("我想吃阿廖沙蛋糕", True),                   # 子串误伤可接受（宁可多回）
        ("今天天气不错", False),
        ("", False),
        (None, False),
    ]
    for text, expect in cases:
        got = wxbot._name_called(cfg, text)
        assert got == expect, (text, got, expect)
    # own_nicknames 命中
    cfg2 = {"reply": {"call_names": []}, "own_nicknames": ["Tony"], "llm": {}}
    assert wxbot._name_called(cfg2, "tony 帮我看看") is True   # 大小写不敏感
    # mention_names 命中 + 空配置不炸
    cfg3 = {"reply": {"group": {"mention_names": ["小猫"]}}, "llm": {}}
    assert wxbot._name_called(cfg3, "小猫快回") is True
    assert wxbot._name_called({"reply": {}, "llm": {}}, "随便说啥") is False
    print("name detection OK")

    print("ALL PASSED ✔")


if __name__ == "__main__":
    main()
