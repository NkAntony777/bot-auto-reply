# -*- coding: utf-8 -*-
"""wxbot_genimg - StepFun 文生图客户端（step_plan 套餐内，与 LLM 同 key 同域名）

实测（2026-08-18）：
  POST https://api.stepfun.com/step_plan/v1/images/generations
  model=step-image-edit-2  prompt=中文  response_format=b64_json
  cfg_scale=1.0 steps=8 text_mode=true → 7.4s 出 1024x1024 PNG

用法（agent 工具层）：
    path, stem = generate(cfg, "一只傲娇奶牛猫")   # 落盘 images/generated/gen_<stem>.png
"""
import base64
import hashlib
import io
import os
import re
import time

DEFAULTS = {
    "enabled": True,
    "model": "step-image-edit-2",
    "steps": 8,
    "cfg_scale": 1.0,
    "timeout_s": 120,
    "max_side": 1024,          # 发微信前缩到该边长以内（省流量/加快上传）
    "keep_files": 40,          # generated/ 目录最多保留张数（旧图自动清理）
}


def _gcfg(cfg):
    out = dict(DEFAULTS)
    out.update(cfg.get("imagegen") or {})
    return out


def generated_dir(cfg) -> str:
    d = (cfg.get("images") or {}).get("dir") or "wxbot_images"
    if not os.path.isabs(d):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), d)
    return os.path.join(d, "generated")


def generate(cfg, prompt: str, seed: int = None):
    """生成一张图并落盘。返回 (绝对路径, 文件stem)；失败抛异常（工具层捕获转文本）。"""
    g = _gcfg(cfg)
    if not g["enabled"]:
        raise RuntimeError("imagegen disabled in config")
    prompt = (prompt or "").strip()[:500]
    if not prompt:
        raise RuntimeError("prompt is empty")
    from curl_cffi import requests as creq
    key = cfg["llm"].get("api_key") or os.environ.get(cfg["llm"].get("api_key_env", ""))
    if not key:
        raise RuntimeError("no StepFun api key")
    payload = {
        "model": g["model"],
        "prompt": prompt,
        "response_format": "b64_json",
        "cfg_scale": g["cfg_scale"],
        "steps": int(g["steps"]),
        "text_mode": True,
    }
    if seed is None:
        # 同 prompt+日期 同 seed：一天内同样的请求可复现（省额度，也方便回归测试）
        seed = int(hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8], 16) % (2 ** 31)
    payload["seed"] = seed

    r = creq.post(
        "https://api.stepfun.com/step_plan/v1/images/generations",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, impersonate="chrome", timeout=int(g["timeout_s"]))
    if r.status_code != 200:
        raise RuntimeError(f"stepfun imagegen HTTP {r.status_code}: {r.text[:150]}")
    b64 = (r.json().get("data") or [{}])[0].get("b64_json")
    if not b64:
        raise RuntimeError("empty b64_json in response")

    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    max_side = int(g["max_side"])
    if max(img.size) > max_side:
        scale = max_side / max(img.size)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))

    d = generated_dir(cfg)
    os.makedirs(d, exist_ok=True)
    if re.search(r"[^\x00-\x7f]", prompt):   # 中文 prompt → hash 短名（文件名安全）
        stem = "gen_" + hashlib.md5(prompt.encode("utf-8")).hexdigest()[:12]
    else:
        stem = ("gen_" + re.sub(r"[^\w]+", "_", prompt).strip("_"))[:32] or "gen_img"
    path = os.path.join(d, f"{stem}.png")
    n = 1
    while os.path.exists(path):   # 同 prompt 再生成：加序号不覆盖
        path = os.path.join(d, f"{stem}_{n}.png")
        n += 1
    img.save(path, "PNG")

    _cleanup(d, int(g["keep_files"]))
    return path, os.path.splitext(os.path.basename(path))[0]


def _cleanup(d: str, keep: int):
    try:
        files = sorted(
            (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".png")),
            key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import wxbot
    t0 = time.time()
    p, stem = generate(wxbot.load_config(),
                       sys.argv[1] if len(sys.argv) > 1 else "一只傲娇的奶牛猫巫师")
    print(f"OK {time.time() - t0:.1f}s -> {p} (stem={stem})")
