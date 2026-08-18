# -*- coding: utf-8 -*-
"""wxbot_tts - StepFun 语音合成（step_plan 套餐内，与 LLM/生图同 key 同域名）

实测（2026-08-18）：
  POST https://api.stepfun.com/step_plan/v1/audio/speech
  model=stepaudio-2.5-tts  voice=tianmeinvsheng  instruction=风格控制
  response_format=mp3 → 2.4s 出 78KB mp3（二进制流）

可用音色（实测）：cixingnansheng(磁性男声) / tianmeinvsheng(甜美女声) /
wenrounvsheng(温柔女声) / qingchunshaonv(青春少女)；instruction 支持情绪风格
（如"扮演傲娇的猫娘，语气俏皮"）。

发送形态：微信 PC 端没有原生语音气泡，走 mp3 文件卡片（wxmini2.send_file，
CF_HDROP 剪贴板粘贴），群内可内联播放。
"""
import hashlib
import os
import re
import time

DEFAULTS = {
    "enabled": True,
    "model": "stepaudio-2.5-tts",
    # 阿廖沙人设：男生正太少年音（站主钦定），性格聪明帅气从容。vibrant-youth=
    # 元气青年，instruction 往正太少年感+自信方向压；备选 yuanqinansheng。
    "voice": "vibrant-youth",
    "instruction": "正太少年音，男孩，聪明帅气，从容自信，语速正常",
    "response_format": "mp3",
    "timeout_s": 120,
    "keep_files": 40,
    "max_chars": 200,          # 单条语音文本上限（太长发不出去也没人听）
}


def _tcfg(cfg):
    out = dict(DEFAULTS)
    out.update(cfg.get("tts") or {})
    return out


def audio_dir(cfg) -> str:
    d = (cfg.get("images") or {}).get("dir") or "wxbot_images"
    if not os.path.isabs(d):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), d)
    return os.path.join(d, "audio")


def synthesize(cfg, text: str, voice: str = None, instruction: str = None):
    """合成一条 mp3 并落盘 wxbot_images/audio/gen_a_<hash>.mp3。
    返回 (绝对路径, stem)；失败抛异常（工具层捕获转文本）。"""
    t = _tcfg(cfg)
    if not t["enabled"]:
        raise RuntimeError("tts disabled in config")
    text = re.sub(r"\s+", " ", (text or "")).strip()[:t["max_chars"]]
    if not text:
        raise RuntimeError("text is empty")
    # 括号动作描写（猫设回复常有）念出来很怪，剥掉
    text = re.sub(r"[（(][^）)]{1,20}[）)]", "", text).strip()
    if not text:
        raise RuntimeError("剥掉动作描写后没有可念的文本")

    from curl_cffi import requests as creq
    key = cfg["llm"].get("api_key") or os.environ.get(cfg["llm"].get("api_key_env", ""))
    if not key:
        raise RuntimeError("no StepFun api key")
    r = creq.post(
        "https://api.stepfun.com/step_plan/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": t["model"], "input": text,
              "voice": voice or t["voice"],
              "instruction": instruction if instruction is not None else t["instruction"],
              "response_format": t["response_format"]},
        impersonate="chrome", timeout=int(t["timeout_s"]))
    if r.status_code != 200:
        raise RuntimeError(f"tts HTTP {r.status_code}: {r.text[:120]}")
    if len(r.content) < 1000:
        raise RuntimeError(f"tts 音频过短({len(r.content)}B)，可能合成失败")

    d = audio_dir(cfg)
    os.makedirs(d, exist_ok=True)
    stem = "gen_a_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(d, f"{stem}.mp3")
    n = 1
    while os.path.exists(path):
        path = os.path.join(d, f"{stem}_{n}.mp3")
        n += 1
    with open(path, "wb") as f:
        f.write(r.content)
    _cleanup(d, int(t["keep_files"]))
    return path, os.path.splitext(os.path.basename(path))[0]


def _cleanup(d: str, keep: int):
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".mp3")),
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
    p, stem = synthesize(wxbot.load_config(),
                         sys.argv[1] if len(sys.argv) > 1 else "（飞机耳）哼唧，本喵会说话了喵，喵喵喵")
    print(f"OK {time.time() - t0:.1f}s -> {p} (stem={stem})")
