# -*- coding: utf-8 -*-
"""State 去重机制回归：已回复判定必须按消息身份（sort_seq），不按内容。

2026-08-19 修复场景：群友把以前发过的一模一样的台词再发一遍
（如「沙沙出来」），旧内容指纹机制误判「已回复过」直接跳过。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wxbot


class MsgKeyTests(unittest.TestCase):
    def test_msg_key_prefers_id_then_ts(self):
        self.assertEqual(wxbot._msg_key({"id": 5001, "ts": 1787104573}), "#5001")
        self.assertEqual(wxbot._msg_key({"id": 0, "ts": 5}), "#5")  # id 为 0/缺失时退化到 ts
        self.assertEqual(wxbot._msg_key({"ts": 1787104573}), "#1787104573")
        self.assertEqual(wxbot._msg_key({}), "#?")

    def test_same_text_new_message_is_not_replied(self):
        with tempfile.TemporaryDirectory() as d:
            st = wxbot.State(os.path.join(d, "state.json"))
            old = {"id": 5001, "kind": "text", "text": "沙沙出来",
                   "side": "other", "sender": "群友", "ts": 1787100000}
            new = {"id": 5099, "kind": "text", "text": "沙沙出来",
                   "side": "other", "sender": "群友", "ts": 1787104573}
            st.mark_replied("阿布菠萝终极粉丝后援团", wxbot._msg_key(old))
            # 老消息本身仍判已回复
            self.assertTrue(st.replied_to("阿布菠萝终极粉丝后援团", wxbot._msg_key(old)))
            # 同文本的新消息必须不再命中
            self.assertFalse(st.replied_to("阿布菠萝终极粉丝后援团", wxbot._msg_key(new)))

    def test_change_detection_fp_distinguishes_same_second_same_text(self):
        a = {"id": 111, "ts": 100, "side": "other", "text": "在吗"}
        b = {"id": 112, "ts": 100, "side": "other", "text": "在吗"}
        fa = f"{a.get('id', '')}|{a.get('ts')}|{a.get('side')}|{a.get('text', '')[:60]}"
        fb = f"{b.get('id', '')}|{b.get('ts')}|{b.get('side')}|{b.get('text', '')[:60]}"
        self.assertNotEqual(fa, fb)

    def test_read_chat_db_msgs_carry_id(self):
        """read_chat_db 输出必须带唯一 id（sort_seq 优先）——去重键的来源。"""
        msgs = [
            {"local_id": 7, "type": "文本", "sender_id": "sid1",
             "create_time": 1787104573, "content": "沙沙出来", "sort_seq": 5001},
        ]
        fake_db = type("FakeDB", (), {
            "wxid": "self", "get_messages": staticmethod(lambda username, limit: list(msgs)),
        })()
        orig = wxbot.wx._get_db
        wxbot.wx._get_db = lambda: fake_db
        try:
            out = wxbot.wx.read_chat_db("chatroom@chatroom", limit=5)
        finally:
            wxbot.wx._get_db = orig
        self.assertTrue(out)
        self.assertEqual(out[0]["id"], 5001)
        self.assertEqual(out[0]["text"], "沙沙出来")


if __name__ == "__main__":
    unittest.main()
