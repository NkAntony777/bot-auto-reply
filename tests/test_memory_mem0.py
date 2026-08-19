# -*- coding: utf-8 -*-
"""mem0 记忆系统联调测试（真实 API：提取 LLM + MiniMax embo-01 embedding + 本地 qdrant）。

用法：
    .venv/Scripts/python.exe -X utf8 tests/test_memory_mem0.py [--keep]

    --keep：结束后保留测试数据（默认删除，保持记忆库干净）
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wxbot_memory as wm

TEST_USER = "联调测试-记忆系统"


def main():
    with io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "wxbot_config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    assert wm.mem0_enabled(cfg), "memory.backend 应为 mem0"

    print("== 1. mem0_add：喂两轮带事实的聊天 ==")
    ok = wm.mem0_add(cfg, TEST_USER, [
        "对方: 我下个月要去成都出差，大概待一周",
        "我: 好呀，记住了，成都出差一周~",
        "对方: 对了我家猫叫煤球，三岁了，特别能吃",
        "我: 煤球哈哈，三岁正是壮年",
    ])
    assert ok, "mem0_add 返回 False（引擎不可用或消息为空）"

    print("\n== 2. mem0_search：语义检索（换说法，不出现原词） ==")
    hits = wm.mem0_search(cfg, TEST_USER, "他家宠物的名字和年纪是多少")
    print(hits or "(无结果)")
    assert "煤球" in hits, "检索应命中「煤球」相关记忆"

    hits2 = wm.mem0_search(cfg, TEST_USER, "他最近有什么出行安排")
    print(hits2 or "(无结果)")
    assert "成都" in hits2, "检索应命中「成都出差」相关记忆"

    print("\n== 3. 记忆更新：同一事实变化应 UPDATE 而不是无限追加 ==")
    wm.mem0_add(cfg, TEST_USER, [
        "对方: 通知一下，我出差改成下周了，不去成都改去重庆",
        "我: 收到，重庆之行安排上",
    ])
    eng = wm._get_engine(cfg)
    texts = [m["memory"] for m in (eng.get_all(filters={"user_id": TEST_USER}) or {}).get("results") or []]
    print("\n".join(f"  - {t}" for t in texts))
    chengdu = [t for t in texts if "成都" in t and "重庆" not in t and "改" not in t]
    if chengdu:
        print("⚠️ 旧「成都」记忆仍在（mem0 可能保留旧事实，靠 updated_at 区分），人工确认：", chengdu)

    print("\n== 4. memory_inject：完整注入格式（markdown 层 + 语义层） ==")
    injected = wm.memory_inject(cfg, TEST_USER, query="他家的猫怎么样")
    print(injected or "(空)")
    assert "相关记忆" in injected and "煤球" in injected

    print("\n== 5. 隔离性：别的对话名检索不到这个对话的记忆 ==")
    other = wm.mem0_search(cfg, "联调测试-另一个对话", "他家宠物的名字和年纪")
    assert other == "", f"隔离失败：捞到了别人的记忆 {other!r}"
    print("OK 隔离正常")

    if "--keep" not in sys.argv:
        print("\n== 清理测试数据 ==")
        eng.delete_all(user_id=TEST_USER)
        print("已删除", TEST_USER)

    print("\nALL PASSED ✔")


if __name__ == "__main__":
    main()
