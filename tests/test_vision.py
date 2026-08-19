import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wxbot


class VisionTests(unittest.TestCase):
    def test_content_accepts_openai_content_parts(self):
        data = {"choices": [{"message": {"content": [
            {"type": "text", "text": "最终答案：一张模型排行榜截图。"},
        ]}}]}
        self.assertEqual("一张模型排行榜截图。", wxbot._vision_content(data))

    def test_retries_transient_tls_error(self):
        cfg = {"vision": {
            "enabled": True, "base_url": "https://example.test/v1",
            "model": "vision-model", "api_key": "test", "retries": 1,
        }}
        good = {"choices": [{"message": {"content": "一张模型排行榜截图。"}}]}
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image:
            image.write(b"test-image")
            image_path = image.name
        try:
            with mock.patch.object(
                wxbot, "_http_post_json",
                side_effect=[RuntimeError("TLS connect error"), good],
            ) as post, mock.patch.object(wxbot.time, "sleep"):
                self.assertEqual("一张模型排行榜截图。", wxbot.vision_describe(cfg, image_path))
                self.assertEqual(2, post.call_count)
        finally:
            os.remove(image_path)

    def test_fallback_runs_after_primary_failure(self):
        cfg = {"vision": {
            "enabled": True, "base_url": "https://primary.test/v1",
            "model": "primary", "api_key": "test", "retries": 0,
            "fallbacks": [{
                "base_url": "https://fallback.test/v1", "model": "fallback",
                "api_key": "test", "retries": 0,
            }],
        }}
        good = {"choices": [{"message": {"content": "备用通道识图成功。"}}]}
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as image:
            image.write(b"test-image")
            image_path = image.name
        try:
            with mock.patch.object(wxbot, "_http_post_json", side_effect=[RuntimeError("bad request"), good]) as post:
                self.assertEqual("备用通道识图成功。", wxbot.vision_describe(cfg, image_path))
                self.assertEqual(2, post.call_count)
        finally:
            os.remove(image_path)


if __name__ == "__main__":
    unittest.main()
