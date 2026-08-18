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
    # 元气青年。instruction 是音高关键：实测不压年龄感会掉到 139Hz 大叔区，
    # 压正太童声可到 212Hz 少年区（2026-08-18 F0 实测，见 _voice_test/）。
    "voice": "vibrant-youth",
    "instruction": "正太童声，10岁小男孩，声音清亮细，绝不是大叔音",
    "response_format": "mp3",
    "timeout_s": 120,
    "keep_files": 40,
    "max_chars": 200,          # 单条语音文本上限（太长发不出去也没人听）
    "pitch_target_hz": 218.0,  # 合成后实测基频低于此值则数字升调到目标（0=关闭）
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


def _median_f0(x, sr: int) -> float:
    """浊音帧自相关基频中位数（Hz）。x: float 单声道 [-1,1]。"""
    import numpy as np
    frame = int(sr * 0.04)
    f0s = []
    for i in range(0, len(x) - frame, frame // 2):
        seg = x[i:i + frame]
        if float(np.sqrt((seg ** 2).mean())) < 0.02:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[frame - 1:]
        ac = ac / (ac[0] + 1e-9)
        lo, hi = int(sr / 400), int(sr / 70)   # 70~400Hz 搜索窗
        if hi >= len(ac):
            continue
        peak = lo + int(np.argmax(ac[lo:hi]))
        if ac[peak] > 0.35:
            f0s.append(sr / peak)
    return float(np.median(f0s)) if f0s else 0.0


def _ensure_pitch(pcm: bytes, target_hz: float):
    """wav 字节 → (处理后的 wav 字节, 实测F0, 是否升调)。
    TTS 同配置音高波动大（实测 140~212Hz），instruction 压不住——低于目标就
    线性插值重采样升调（音频等比变短=加速=升调；instruction 已带'语速稍慢'预补偿）。"""
    import io
    import numpy as np
    import wave
    with wave.open(io.BytesIO(pcm), "rb") as w:
        sr, nch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        return pcm, 0.0, 1.0
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    f0 = _median_f0(x, sr)
    if target_hz <= 0 or f0 <= 0 or f0 >= target_hz * 0.97:
        return pcm, f0, 1.0
    ratio = min(target_hz / f0, 1.6)
    n_out = max(1, int(len(x) / ratio))
    y = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)
    y = np.clip(y, -1.0, 1.0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())
    return buf.getvalue(), f0, ratio


def synthesize(cfg, text: str, voice: str = None, instruction: str = None):
    """合成一条语音并落盘 wxbot_images/audio/gen_a_<hash>.wav。
    合成用 wav（可测基频），实测低于 pitch_target_hz 时数字升调到目标，
    保证正太少年音寄存器稳定。返回 (绝对路径, stem)；失败抛异常。"""
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
    base_inst = instruction if instruction is not None else t["instruction"]
    if float(t.get("pitch_target_hz", 0) or 0) > 0:
        base_inst += "，语速稍慢"   # 升调会加速，提前补偿语速
    r = creq.post(
        "https://api.stepfun.com/step_plan/v1/audio/speech",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": t["model"], "input": text,
              "voice": voice or t["voice"],
              "instruction": base_inst,
              "response_format": "wav"},
        impersonate="chrome", timeout=int(t["timeout_s"]))
    if r.status_code != 200:
        raise RuntimeError(f"tts HTTP {r.status_code}: {r.text[:120]}")
    if len(r.content) < 1000:
        raise RuntimeError(f"tts 音频过短({len(r.content)}B)，可能合成失败")
    content, f0, ratio = _ensure_pitch(r.content, float(t.get("pitch_target_hz", 0) or 0))
    if f0:
        note = f" -> pitch x{ratio:.2f} = {f0 * ratio:.0f}Hz" if ratio > 1.0 else " (in range)"
        print(f"[tts] raw F0={f0:.0f}Hz{note}")

    d = audio_dir(cfg)
    os.makedirs(d, exist_ok=True)
    stem = "gen_a_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(d, f"{stem}.wav")
    n = 1
    while os.path.exists(path):
        path = os.path.join(d, f"{stem}_{n}.wav")
        n += 1
    with open(path, "wb") as f:
        f.write(content)
    _cleanup(d, int(t["keep_files"]))
    return path, os.path.splitext(os.path.basename(path))[0]


def _cleanup(d: str, keep: int):
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith((".mp3", ".wav"))),
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
